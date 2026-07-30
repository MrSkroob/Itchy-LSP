"use strict";
// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
Object.defineProperty(exports, "__esModule", { value: true });
exports.onDidChangeConfiguration = void 0;
exports.createOutputChannel = createOutputChannel;
exports.getConfiguration = getConfiguration;
exports.registerCommand = registerCommand;
exports.isVirtualWorkspace = isVirtualWorkspace;
exports.getWorkspaceFolders = getWorkspaceFolders;
exports.getWorkspaceFolder = getWorkspaceFolder;
/* eslint-disable @typescript-eslint/explicit-module-boundary-types */
/* eslint-disable @typescript-eslint/no-explicit-any */
const vscode_1 = require("vscode");
function createOutputChannel(name) {
    return vscode_1.window.createOutputChannel(name, { log: true });
}
function getConfiguration(config, scope) {
    return vscode_1.workspace.getConfiguration(config, scope);
}
function registerCommand(command, callback, thisArg) {
    return vscode_1.commands.registerCommand(command, callback, thisArg);
}
exports.onDidChangeConfiguration = vscode_1.workspace.onDidChangeConfiguration;
function isVirtualWorkspace() {
    const isVirtual = vscode_1.workspace.workspaceFolders && vscode_1.workspace.workspaceFolders.every((f) => f.uri.scheme !== 'file');
    return !!isVirtual;
}
function getWorkspaceFolders() {
    return vscode_1.workspace.workspaceFolders ?? [];
}
function getWorkspaceFolder(uri) {
    return vscode_1.workspace.getWorkspaceFolder(uri);
}
//# sourceMappingURL=vscodeapi.js.map