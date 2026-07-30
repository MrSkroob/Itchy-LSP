"use strict";
// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
Object.defineProperty(exports, "__esModule", { value: true });
exports.getLSClientTraceLevel = getLSClientTraceLevel;
exports.getProjectRoot = getProjectRoot;
const fs = require("fs-extra");
const path = require("path");
const vscode_1 = require("vscode");
const node_1 = require("vscode-jsonrpc/node");
const vscodeapi_1 = require("./vscodeapi");
function logLevelToTrace(logLevel) {
    switch (logLevel) {
        case vscode_1.LogLevel.Error:
        case vscode_1.LogLevel.Warning:
        case vscode_1.LogLevel.Info:
            return node_1.Trace.Messages;
        case vscode_1.LogLevel.Debug:
        case vscode_1.LogLevel.Trace:
            return node_1.Trace.Verbose;
        case vscode_1.LogLevel.Off:
        default:
            return node_1.Trace.Off;
    }
}
function getLSClientTraceLevel(channelLogLevel, globalLogLevel) {
    if (channelLogLevel === vscode_1.LogLevel.Off) {
        return logLevelToTrace(globalLogLevel);
    }
    if (globalLogLevel === vscode_1.LogLevel.Off) {
        return logLevelToTrace(channelLogLevel);
    }
    const level = logLevelToTrace(channelLogLevel <= globalLogLevel ? channelLogLevel : globalLogLevel);
    return level;
}
async function getProjectRoot() {
    const workspaces = (0, vscodeapi_1.getWorkspaceFolders)();
    if (workspaces.length === 0) {
        return {
            uri: vscode_1.Uri.file(process.cwd()),
            name: path.basename(process.cwd()),
            index: 0,
        };
    }
    else if (workspaces.length === 1) {
        return workspaces[0];
    }
    else {
        let rootWorkspace = workspaces[0];
        let root = undefined;
        for (const w of workspaces) {
            if (await fs.pathExists(w.uri.fsPath)) {
                root = w.uri.fsPath;
                rootWorkspace = w;
                break;
            }
        }
        for (const w of workspaces) {
            if (root && root.length > w.uri.fsPath.length && (await fs.pathExists(w.uri.fsPath))) {
                root = w.uri.fsPath;
                rootWorkspace = w;
            }
        }
        return rootWorkspace;
    }
}
//# sourceMappingURL=utilities.js.map