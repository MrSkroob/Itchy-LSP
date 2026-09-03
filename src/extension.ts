// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

import * as vscode from 'vscode';
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
import { watch } from 'fs';


interface TargetFiles {
    uri: string;
    costumes: String[];
    sounds: String[]
}


async function getTargets(): Promise<vscode.Uri[]> {
    const targets: vscode.Uri[] = [];
    const editor = vscode.window.activeTextEditor;

    if (!editor) {
        return targets;
    }

    const fileUri = editor.document.uri;

    if (!fileUri) {
        return targets;
    }

    const workspaceFolder = commands.resolveVariables(vscode.workspace.getConfiguration('Itchy LSP').get('cwd', '${workspaceFolder}'), fileUri)
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
                targetEntries
                    .filter(([, type]) => type === vscode.FileType.Directory)
                    .map(([name]) => name)
            );

            if (directories.has('costumes') && directories.has('sounds')) {
                targets.push(targetUri);
            }
        } catch {
            // ignore
        }
    }

    return targets
}


async function  sendInitialTargetFiles(client: LanguageClient | undefined): Promise<void> {
    if (!client) {
        return;
    }
    
    const targetUris = await getTargets();

    const targets: TargetFiles[] =  await Promise.all(
        targetUris.map(async (targetUri) => {
            const [costumes, sounds] = await getTargetFiles(targetUri);

            return {
                uri: targetUri.toString(),
                costumes: costumes,
                sounds: sounds
            }
        })
    )

    await client.sendNotification('itchy/targetFiles', {
        targets
    });
}

async function sendTargetFilesChanged(client: LanguageClient, targetUri: vscode.Uri): Promise<void> {
    try {
        const [costumes, sounds] = await getTargetFiles(targetUri);

        await client.sendNotification('itchy/targetFilesChanged', {
            uri: targetUri.toString(),
            costumes,
            sounds
        })
    } catch {

    }
}


let lsClient: LanguageClient | undefined;
let isRestarting = false;
let restartTimer: NodeJS.Timeout | undefined;
export async function activate(context: vscode.ExtensionContext): Promise<void> {
    // This is required to get server name and module. This should be
    // the first thing that we do in this extension.
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

    // Log Server information
    traceLog(`Name: ${serverInfo.name}`);
    traceLog(`Module: ${serverInfo.module}`);
    traceVerbose(`Full Server Info: ${JSON.stringify(serverInfo)}`);

    const restartAndSyncServer = async () => {
        lsClient = await restartServer(
            serverId,
            serverName,
            outputChannel,
            lsClient,
        );

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

    // file watchers
    for (const workspaceFolder of vscode.workspace.workspaceFolders ?? []) {
        const watcher = vscode.workspace.createFileSystemWatcher(
            new vscode.RelativePattern(
                workspaceFolder,
                '**/{costumes,sounds}/*'
            )
        );

        const updateTarget = async (fileUri: vscode.Uri) => {
            if (!lsClient) {
                return;
            }

            const targetUri = vscode.Uri.joinPath(fileUri, '..', '..');
            await sendTargetFilesChanged(lsClient, targetUri);
        }

        context.subscriptions.push(
            watcher,
            watcher.onDidCreate(updateTarget),
            watcher.onDidDelete(updateTarget)
        )
    }

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
            traceLog(`Python extension loading`);
            await initializePython(context.subscriptions);
            traceLog(`Python extension loaded`);
        } else {
            await runServer();
        }
    });
}

export async function deactivate(): Promise<void> {
    if (lsClient) {
        try {
            await lsClient.stop();
        } catch (ex) {
            traceError(`Server: Stop failed: ${ex}`);
        }
    }
}
