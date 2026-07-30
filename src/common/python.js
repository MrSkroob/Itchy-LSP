"use strict";
// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
Object.defineProperty(exports, "__esModule", { value: true });
exports.onDidChangePythonInterpreter = void 0;
exports.initializePython = initializePython;
exports.resolveInterpreter = resolveInterpreter;
exports.getInterpreterDetails = getInterpreterDetails;
exports.getDebuggerPath = getDebuggerPath;
exports.runPythonExtensionCommand = runPythonExtensionCommand;
exports.checkVersion = checkVersion;
/* eslint-disable @typescript-eslint/naming-convention */
const vscode_1 = require("vscode");
const logging_1 = require("./log/logging");
const python_extension_1 = require("@vscode/python-extension");
const python_environments_1 = require("@vscode/python-environments");
const onDidChangePythonInterpreterEvent = new vscode_1.EventEmitter();
exports.onDidChangePythonInterpreter = onDidChangePythonInterpreterEvent.event;
let _api;
async function getPythonExtensionAPI() {
    if (_api) {
        return _api;
    }
    _api = await python_extension_1.PythonExtension.api();
    return _api;
}
let _envsApi;
async function getEnvironmentsExtensionAPI() {
    if (_envsApi) {
        return _envsApi;
    }
    try {
        _envsApi = await python_environments_1.PythonEnvironments.api();
    }
    catch {
        return undefined;
    }
    return _envsApi;
}
async function initializePython(disposables) {
    try {
        // // Prefer the Python Environments extension if it's available, as it provides a more comprehensive view of the available environments.
        const envsApi = await getEnvironmentsExtensionAPI();
        if (envsApi) {
            disposables.push(envsApi.onDidChangeEnvironment((e) => {
                onDidChangePythonInterpreterEvent.fire({
                    path: e.new
                        ? [e.new.execInfo.run.executable]
                        : undefined,
                    resource: e.uri,
                });
            }));
            (0, logging_1.traceLog)('Waiting for interpreter from python environments extension.');
            onDidChangePythonInterpreterEvent.fire(await getInterpreterDetails());
            return;
        }
        // Fall back to legacy ms-python.python extension API
        const api = await getPythonExtensionAPI();
        if (api) {
            disposables.push(api.environments.onDidChangeActiveEnvironmentPath((e) => {
                const resource = e.resource instanceof vscode_1.Uri ? e.resource : e.resource?.uri;
                onDidChangePythonInterpreterEvent.fire({ path: [e.path], resource });
            }));
            (0, logging_1.traceLog)('Waiting for interpreter from python extension.');
            onDidChangePythonInterpreterEvent.fire(await getInterpreterDetails());
        }
    }
    catch (error) {
        (0, logging_1.traceError)('Error initializing python: ', error);
    }
}
async function resolveInterpreter(interpreter) {
    const api = await getPythonExtensionAPI();
    return api?.environments.resolveEnvironment(interpreter[0]);
}
async function getInterpreterDetails(resource) {
    // Prefer the Python Environments extension if it's available, as it provides a more comprehensive view of the available environments.
    const envsApi = await getEnvironmentsExtensionAPI();
    if (envsApi) {
        const environment = await envsApi.getEnvironment(resource);
        if (environment) {
            return {
                path: [environment.execInfo.run.executable],
                resource,
            };
        }
        return { path: undefined, resource };
    }
    // Fall back to legacy ms-python.python extension API
    const api = await getPythonExtensionAPI();
    const environment = await api?.environments.resolveEnvironment(api?.environments.getActiveEnvironmentPath(resource));
    if (environment?.executable.uri && checkVersion(environment)) {
        return { path: [environment?.executable.uri.fsPath], resource };
    }
    return { path: undefined, resource };
}
async function getDebuggerPath() {
    const api = await getPythonExtensionAPI();
    return api?.debug.getDebuggerPackagePath();
}
async function runPythonExtensionCommand(command, ...rest) {
    await getPythonExtensionAPI();
    return await vscode_1.commands.executeCommand(command, ...rest);
}
function checkVersion(resolved) {
    const version = resolved?.version;
    if (version?.major === 3 && version?.minor >= 10) {
        return true;
    }
    (0, logging_1.traceError)(`Python version ${version?.major}.${version?.minor} is not supported.`);
    (0, logging_1.traceError)(`Selected python path: ${resolved?.executable.uri?.fsPath}`);
    (0, logging_1.traceError)('Supported versions are 3.10 and above.');
    return false;
}
//# sourceMappingURL=python.js.map