"use strict";
// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = require("vscode");
const logging_1 = require("./common/log/logging");
const python_1 = require("./common/python");
const server_1 = require("./common/server");
const settings_1 = require("./common/settings");
const setup_1 = require("./common/setup");
const constants_1 = require("./common/constants");
const utilities_1 = require("./common/utilities");
const vscodeapi_1 = require("./common/vscodeapi");
let lsClient;
let isRestarting = false;
let restartTimer;
async function activate(context) {
    // This is required to get server name and module. This should be
    // the first thing that we do in this extension.
    const serverInfo = (0, setup_1.loadServerDefaults)();
    const serverName = serverInfo.name;
    const serverId = serverInfo.module;
    // Setup logging
    const outputChannel = (0, vscodeapi_1.createOutputChannel)(serverName);
    context.subscriptions.push(outputChannel, (0, logging_1.registerLogger)(outputChannel));
    const changeLogLevel = async (c, g) => {
        const level = (0, utilities_1.getLSClientTraceLevel)(c, g);
        await lsClient?.setTrace(level);
    };
    context.subscriptions.push(outputChannel.onDidChangeLogLevel(async (e) => {
        await changeLogLevel(e, vscode.env.logLevel);
    }), vscode.env.onDidChangeLogLevel(async (e) => {
        await changeLogLevel(outputChannel.logLevel, e);
    }));
    // Log Server information
    (0, logging_1.traceLog)(`Name: ${serverInfo.name}`);
    (0, logging_1.traceLog)(`Module: ${serverInfo.module}`);
    (0, logging_1.traceVerbose)(`Full Server Info: ${JSON.stringify(serverInfo)}`);
    const runServer = async () => {
        if (isRestarting) {
            if (restartTimer) {
                clearTimeout(restartTimer);
            }
            restartTimer = setTimeout(runServer, constants_1.LS_SERVER_RESTART_DELAY);
            return;
        }
        isRestarting = true;
        try {
            const interpreter = (0, settings_1.getInterpreterFromSetting)(serverId);
            if (interpreter && interpreter.length > 0) {
                if ((0, python_1.checkVersion)(await (0, python_1.resolveInterpreter)(interpreter))) {
                    (0, logging_1.traceVerbose)(`Using interpreter from ${serverInfo.module}.interpreter: ${interpreter.join(' ')}`);
                    lsClient = await (0, server_1.restartServer)(serverId, serverName, outputChannel, lsClient);
                }
                return;
            }
            const interpreterDetails = await (0, python_1.getInterpreterDetails)();
            if (interpreterDetails.path) {
                (0, logging_1.traceVerbose)(`Using interpreter from Python extension: ${interpreterDetails.path.join(' ')}`);
                lsClient = await (0, server_1.restartServer)(serverId, serverName, outputChannel, lsClient);
                return;
            }
            (0, logging_1.traceError)('Python interpreter missing:\r\n' +
                '[Option 1] Select python interpreter using the ms-python.python.\r\n' +
                `[Option 2] Set an interpreter using "${serverId}.interpreter" setting.\r\n` +
                'Please use Python 3.10 or greater.');
        }
        finally {
            isRestarting = false;
        }
    };
    context.subscriptions.push((0, python_1.onDidChangePythonInterpreter)(async () => {
        await runServer();
    }), (0, vscodeapi_1.onDidChangeConfiguration)(async (e) => {
        if ((0, settings_1.checkIfConfigurationChanged)(e, serverId)) {
            await runServer();
        }
    }), (0, vscodeapi_1.registerCommand)(`${serverId}.restart`, async () => {
        await runServer();
    }));
    setImmediate(async () => {
        const interpreter = (0, settings_1.getInterpreterFromSetting)(serverId);
        if (interpreter === undefined || interpreter.length === 0) {
            (0, logging_1.traceLog)(`Python extension loading`);
            await (0, python_1.initializePython)(context.subscriptions);
            (0, logging_1.traceLog)(`Python extension loaded`);
        }
        else {
            await runServer();
        }
    });
}
async function deactivate() {
    if (lsClient) {
        try {
            await lsClient.stop();
        }
        catch (ex) {
            (0, logging_1.traceError)(`Server: Stop failed: ${ex}`);
        }
    }
}
//# sourceMappingURL=extension.js.map