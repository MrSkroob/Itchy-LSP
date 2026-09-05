// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

import * as vscode from 'vscode';
import * as path from 'path';

import * as commands from './common/commands';
import { LanguageClient } from 'vscode-languageclient/node';
import { registerLogger, traceError, traceLog, traceVerbose } from './common/log/logging';
import {
    checkVersion,
    getInterpreterDetails,
    initializePython,
    onDidChangePythonInterpreter,
    resolveInterpreter,
} from './common/python';
import { restartServer } from './common/server';
import { checkIfConfigurationChanged, getInterpreterFromSetting } from './common/settings';
import { loadServerDefaults } from './common/setup';
import { LS_SERVER_RESTART_DELAY } from './common/constants';
import { getLSClientTraceLevel, getTargetFiles } from './common/utilities';
import { createOutputChannel, onDidChangeConfiguration, registerCommand } from './common/vscodeapi';

interface AssetRename {
    resourceType: 'costume' | 'sound';
    oldName: string;
    newName: string;
}

interface TargetFiles {
    uri: string;
    costumes: String[];
    sounds: String[];
    renames?: AssetRename[];
}

async function getTargets(): Promise<vscode.Uri[]> {
    const targets: vscode.Uri[] = [];
    const editor = vscode.window.activeTextEditor;

    if (!editor) {
        return targets;
    }

    const fileUri = editor.document.uri;

    const workspaceFolder = commands.resolveVariables(
        vscode.workspace.getConfiguration('Itchy LSP').get('cwd', '${workspaceFolder}'),
        fileUri,
    );

    const folderUri = vscode.Uri.file(workspaceFolder);
    const entries = await vscode.workspace.fs.readDirectory(folderUri);

    for (const [name, type] of entries) {
        if (type !== vscode.FileType.Directory) {
            continue;
        }

        const targetUri = vscode.Uri.joinPath(folderUri, name);

        try {
            const targetEntries = await vscode.workspace.fs.readDirectory(targetUri);

            const directories = new Set(
                targetEntries.filter(([, type]) => type === vscode.FileType.Directory).map(([name]) => name),
            );

            if (directories.has('costumes') && directories.has('sounds')) {
                targets.push(targetUri);
            }
        } catch {
            // Ignore directories which disappear while scanning.
        }
    }

    return targets;
}

async function sendInitialTargetFiles(client: LanguageClient | undefined): Promise<void> {
    if (!client) {
        return;
    }

    const targetUris = await getTargets();

    const targets: TargetFiles[] = await Promise.all(
        targetUris.map(async (targetUri) => {
            const [costumes, sounds] = await getTargetFiles(targetUri);

            return {
                uri: targetUri.toString(),
                costumes,
                sounds,
            };
        }),
    );

    await client.sendNotification('itchy/targetFiles', {
        targets,
    });
}

async function sendTargetFilesChanged(
    client: LanguageClient,
    targetUri: vscode.Uri,
    renames: AssetRename[] = [],
): Promise<void> {
    try {
        const [costumes, sounds] = await getTargetFiles(targetUri);

        const targetFiles: TargetFiles = {
            uri: targetUri.toString(),
            costumes,
            sounds,
        };

        if (renames.length > 0) {
            targetFiles.renames = renames;
        }

        await client.sendNotification('itchy/targetFilesChanged', targetFiles);
    } catch {
        // The target may have been deleted/moved.
    }
}

function getTargetUriFromAsset(assetUri: vscode.Uri): vscode.Uri {
    return vscode.Uri.joinPath(assetUri, '..', '..');
}

function getAssetType(assetUri: vscode.Uri): 'costume' | 'sound' | undefined {
    const parent = path.basename(path.dirname(assetUri.fsPath));

    switch (parent) {
        case 'costumes':
            return 'costume';

        case 'sounds':
            return 'sound';

        default:
            return undefined;
    }
}

function getAssetRename(oldUri: vscode.Uri, newUri: vscode.Uri): AssetRename | undefined {
    const oldType = getAssetType(oldUri);
    const newType = getAssetType(newUri);

    // We only consider it a reference rename when the asset stays
    // the same kind of resource.
    if (oldType === undefined || newType === undefined || oldType !== newType) {
        return undefined;
    }

    const oldTarget = getTargetUriFromAsset(oldUri);
    const newTarget = getTargetUriFromAsset(newUri);

    // Moving an asset between sprites is not just a name change.
    if (oldTarget.toString() !== newTarget.toString()) {
        return undefined;
    }

    const oldName = path.parse(oldUri.fsPath).name;
    const newName = path.parse(newUri.fsPath).name;

    if (oldName === newName) {
        return undefined;
    }

    return {
        resourceType: oldType,
        oldName,
        newName,
    };
}

let lsClient: LanguageClient | undefined;

let isRestarting = false;
let restartTimer: NodeJS.Timeout | undefined;

// -------------------------------------------------------------------------
// Debounced target-file updates
// -------------------------------------------------------------------------

const TARGET_FILES_DEBOUNCE_MS = 100;

const targetUpdateTimers = new Map<string, NodeJS.Timeout>();
const pendingTargetRenames = new Map<string, AssetRename[]>();

function addPendingRename(targetUri: vscode.Uri, rename: AssetRename): void {
    const key = targetUri.toString();

    const renames = pendingTargetRenames.get(key) ?? [];

    // Avoid adding the same rename more than once.
    const alreadyExists = renames.some(
        (existing) =>
            existing.resourceType === rename.resourceType &&
            existing.oldName === rename.oldName &&
            existing.newName === rename.newName,
    );

    if (!alreadyExists) {
        renames.push(rename);
    }

    pendingTargetRenames.set(key, renames);
}

function queueTargetFilesChanged(targetUri: vscode.Uri): void {
    const key = targetUri.toString();

    const existingTimer = targetUpdateTimers.get(key);

    if (existingTimer) {
        clearTimeout(existingTimer);
    }

    const timer = setTimeout(async () => {
        targetUpdateTimers.delete(key);

        const client = lsClient;

        if (!client) {
            pendingTargetRenames.delete(key);
            return;
        }

        const renames = pendingTargetRenames.get(key) ?? [];

        // Remove these before sending so any new filesystem event
        // that occurs while awaiting the notification belongs to
        // the next batch.
        pendingTargetRenames.delete(key);

        await sendTargetFilesChanged(client, targetUri, renames);
    }, TARGET_FILES_DEBOUNCE_MS);

    targetUpdateTimers.set(key, timer);
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
    const serverInfo = loadServerDefaults();
    const serverName = serverInfo.name;
    const serverId = serverInfo.module;

    // Setup logging
    const outputChannel = createOutputChannel(serverName);

    context.subscriptions.push(outputChannel, registerLogger(outputChannel));

    const changeLogLevel = async (c: vscode.LogLevel, g: vscode.LogLevel) => {
        const level = getLSClientTraceLevel(c, g);
        await lsClient?.setTrace(level);
    };

    context.subscriptions.push(
        outputChannel.onDidChangeLogLevel(async (e) => {
            await changeLogLevel(e, vscode.env.logLevel);
        }),

        vscode.env.onDidChangeLogLevel(async (e) => {
            await changeLogLevel(outputChannel.logLevel, e);
        }),
    );

    traceLog(`Name: ${serverInfo.name}`);
    traceLog(`Module: ${serverInfo.module}`);
    traceVerbose(`Full Server Info: ${JSON.stringify(serverInfo)}`);

    const restartAndSyncServer = async () => {
        lsClient = await restartServer(serverId, serverName, outputChannel, lsClient);

        await sendInitialTargetFiles(lsClient);
    };

    const runServer = async () => {
        if (isRestarting) {
            if (restartTimer) {
                clearTimeout(restartTimer);
            }

            restartTimer = setTimeout(runServer, LS_SERVER_RESTART_DELAY);

            return;
        }

        isRestarting = true;

        try {
            const interpreter = getInterpreterFromSetting(serverId);

            if (interpreter && interpreter.length > 0) {
                if (checkVersion(await resolveInterpreter(interpreter))) {
                    traceVerbose(`Using interpreter from ${serverInfo.module}.interpreter: ${interpreter.join(' ')}`);

                    await restartAndSyncServer();
                }

                return;
            }

            const interpreterDetails = await getInterpreterDetails();

            if (interpreterDetails.path) {
                traceVerbose(`Using interpreter from Python extension: ${interpreterDetails.path.join(' ')}`);

                await restartAndSyncServer();
                return;
            }

            traceError(
                'Python interpreter missing:\r\n' +
                    '[Option 1] Select python interpreter using the ms-python.python.\r\n' +
                    `[Option 2] Set an interpreter using "${serverId}.interpreter" setting.\r\n` +
                    'Please use Python 3.10 or greater.',
            );
        } finally {
            isRestarting = false;
        }
    };

    // ---------------------------------------------------------------------
    // Asset create/delete watcher
    // ---------------------------------------------------------------------

    for (const workspaceFolder of vscode.workspace.workspaceFolders ?? []) {
        const watcher = vscode.workspace.createFileSystemWatcher(
            new vscode.RelativePattern(workspaceFolder, '**/{costumes,sounds}/*'),
        );

        const updateTarget = (fileUri: vscode.Uri) => {
            const targetUri = getTargetUriFromAsset(fileUri);

            queueTargetFilesChanged(targetUri);
        };

        context.subscriptions.push(watcher, watcher.onDidCreate(updateTarget), watcher.onDidDelete(updateTarget));
    }

    // ---------------------------------------------------------------------
    // Asset rename handling
    // ---------------------------------------------------------------------

    context.subscriptions.push(
        vscode.workspace.onDidRenameFiles((event) => {
            for (const file of event.files) {
                const oldType = getAssetType(file.oldUri);
                const newType = getAssetType(file.newUri);

                //
                // Refresh the old target if the old URI was an asset.
                //
                if (oldType !== undefined) {
                    const oldTarget = getTargetUriFromAsset(file.oldUri);

                    queueTargetFilesChanged(oldTarget);
                }

                //
                // Refresh the new target if the new URI is an asset.
                //
                if (newType !== undefined) {
                    const newTarget = getTargetUriFromAsset(file.newUri);

                    const rename = getAssetRename(file.oldUri, file.newUri);

                    if (rename) {
                        addPendingRename(newTarget, rename);
                    }

                    queueTargetFilesChanged(newTarget);
                }
            }
        }),
    );

    context.subscriptions.push(
        onDidChangePythonInterpreter(async () => {
            await runServer();
        }),

        onDidChangeConfiguration(async (e: vscode.ConfigurationChangeEvent) => {
            if (checkIfConfigurationChanged(e, serverId)) {
                await runServer();
            }
        }),

        registerCommand(`${serverId}.restart`, async () => {
            await runServer();
        }),

        registerCommand(`${serverId}.createScratchProject`, commands.createScratchProject),

        registerCommand(`${serverId}.addSprite`, commands.addSprite),

        registerCommand(`${serverId}.compile`, commands.compile),

        registerCommand(`${serverId}.compileFile`, () => commands.compileFile(context)),

        registerCommand(`${serverId}.compileProject`, () => commands.compileProject(context)),
    );

    setImmediate(async () => {
        const interpreter = getInterpreterFromSetting(serverId);

        if (interpreter === undefined || interpreter.length === 0) {
            traceLog('Python extension loading');

            await initializePython(context.subscriptions);

            traceLog('Python extension loaded');
        } else {
            await runServer();
        }
    });
}

export async function deactivate(): Promise<void> {
    for (const timer of targetUpdateTimers.values()) {
        clearTimeout(timer);
    }

    targetUpdateTimers.clear();
    pendingTargetRenames.clear();

    if (lsClient) {
        try {
            await lsClient.stop();
        } catch (ex) {
            traceError(`Server: Stop failed: ${ex}`);
        }
    }
}
