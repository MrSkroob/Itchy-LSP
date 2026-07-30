"use strict";
// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
Object.defineProperty(exports, "__esModule", { value: true });
exports.restartServer = restartServer;
const fsapi = require("fs-extra");
const vscode_1 = require("vscode");
const vscode_languageclient_1 = require("vscode-languageclient");
const node_1 = require("vscode-languageclient/node");
const constants_1 = require("./constants");
const logging_1 = require("./log/logging");
const python_1 = require("./python");
const settings_1 = require("./settings");
const utilities_1 = require("./utilities");
const vscodeapi_1 = require("./vscodeapi");
async function createServer(settings, serverId, serverName, outputChannel, initializationOptions) {
    const command = settings.interpreter[0];
    const cwd = settings.cwd;
    // Set debugger path needed for debugging python code.
    const newEnv = { ...process.env };
    const debuggerPath = await (0, python_1.getDebuggerPath)();
    const isDebugScript = await fsapi.pathExists(constants_1.DEBUG_SERVER_SCRIPT_PATH);
    if (newEnv.USE_DEBUGPY && debuggerPath) {
        newEnv.DEBUGPY_PATH = debuggerPath;
    }
    else {
        newEnv.USE_DEBUGPY = 'False';
    }
    // Set import strategy
    newEnv.LS_IMPORT_STRATEGY = settings.importStrategy;
    // Set notification type
    newEnv.LS_SHOW_NOTIFICATION = settings.showNotifications;
    const args = newEnv.USE_DEBUGPY === 'False' || !isDebugScript
        ? settings.interpreter.slice(1).concat([constants_1.SERVER_SCRIPT_PATH])
        : settings.interpreter.slice(1).concat([constants_1.DEBUG_SERVER_SCRIPT_PATH]);
    (0, logging_1.traceInfo)(`Server run command: ${[command, ...args].join(' ')}`);
    const serverOptions = {
        command,
        args,
        options: { cwd, env: newEnv },
    };
    // Options to control the language client
    const clientOptions = {
        // Register the server for python documents
        documentSelector: (0, vscodeapi_1.isVirtualWorkspace)()
            ? [{ language: 'python' }]
            : [
                { scheme: 'file', language: 'python' },
                { scheme: 'untitled', language: 'python' },
                { scheme: 'vscode-notebook', language: 'python' },
                { scheme: 'vscode-notebook-cell', language: 'python' },
            ],
        outputChannel: outputChannel,
        traceOutputChannel: outputChannel,
        revealOutputChannelOn: node_1.RevealOutputChannelOn.Never,
        initializationOptions,
    };
    return new node_1.LanguageClient(serverId, serverName, serverOptions, clientOptions);
}
let _disposables = [];
async function restartServer(serverId, serverName, outputChannel, lsClient) {
    if (lsClient) {
        (0, logging_1.traceInfo)(`Server: Stop requested`);
        try {
            await lsClient.stop();
        }
        catch (ex) {
            (0, logging_1.traceError)(`Server: Stop failed: ${ex}`);
        }
    }
    _disposables.forEach((d) => d.dispose());
    _disposables = [];
    const projectRoot = await (0, utilities_1.getProjectRoot)();
    const workspaceSetting = await (0, settings_1.getWorkspaceSettings)(serverId, projectRoot, true);
    const newLSClient = await createServer(workspaceSetting, serverId, serverName, outputChannel, {
        settings: await (0, settings_1.getExtensionSettings)(serverId, true),
        globalSettings: await (0, settings_1.getGlobalSettings)(serverId, false),
    });
    (0, logging_1.traceInfo)(`Server: Start requested.`);
    _disposables.push(newLSClient.onDidChangeState((e) => {
        switch (e.newState) {
            case vscode_languageclient_1.State.Stopped:
                (0, logging_1.traceVerbose)(`Server State: Stopped`);
                break;
            case vscode_languageclient_1.State.Starting:
                (0, logging_1.traceVerbose)(`Server State: Starting`);
                break;
            case vscode_languageclient_1.State.Running:
                (0, logging_1.traceVerbose)(`Server State: Running`);
                break;
        }
    }));
    try {
        await newLSClient.start();
    }
    catch (ex) {
        (0, logging_1.traceError)(`Server: Start failed: ${ex}`);
        return undefined;
    }
    const level = (0, utilities_1.getLSClientTraceLevel)(outputChannel.logLevel, vscode_1.env.logLevel);
    await newLSClient.setTrace(level);
    return newLSClient;
}
//# sourceMappingURL=server.js.map