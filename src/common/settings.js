"use strict";
// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
Object.defineProperty(exports, "__esModule", { value: true });
exports.getExtensionSettings = getExtensionSettings;
exports.getInterpreterFromSetting = getInterpreterFromSetting;
exports.getWorkspaceSettings = getWorkspaceSettings;
exports.getGlobalSettings = getGlobalSettings;
exports.checkIfConfigurationChanged = checkIfConfigurationChanged;
const python_1 = require("./python");
const vscodeapi_1 = require("./vscodeapi");
function getExtensionSettings(namespace, includeInterpreter) {
    return Promise.all((0, vscodeapi_1.getWorkspaceFolders)().map((w) => getWorkspaceSettings(namespace, w, includeInterpreter)));
}
function resolveVariables(value, workspace) {
    const substitutions = new Map();
    const home = process.env.HOME || process.env.USERPROFILE;
    if (home) {
        substitutions.set('${userHome}', home);
    }
    if (workspace) {
        substitutions.set('${workspaceFolder}', workspace.uri.fsPath);
    }
    substitutions.set('${cwd}', process.cwd());
    (0, vscodeapi_1.getWorkspaceFolders)().forEach((w) => {
        substitutions.set('${workspaceFolder:' + w.name + '}', w.uri.fsPath);
    });
    return value.map((s) => {
        for (const [key, value] of substitutions) {
            s = s.replace(key, value);
        }
        return s;
    });
}
function getInterpreterFromSetting(namespace, scope) {
    const config = (0, vscodeapi_1.getConfiguration)(namespace, scope);
    return config.get('interpreter');
}
function getCwd(config, workspace) {
    const cwd = config.get('cwd', '${workspaceFolder}');
    return resolveVariables([cwd], workspace)[0];
}
async function getWorkspaceSettings(namespace, workspace, includeInterpreter) {
    const config = (0, vscodeapi_1.getConfiguration)(namespace, workspace.uri);
    let interpreter = [];
    if (includeInterpreter) {
        interpreter = getInterpreterFromSetting(namespace, workspace) ?? [];
        if (interpreter.length === 0) {
            interpreter = (await (0, python_1.getInterpreterDetails)(workspace.uri)).path ?? [];
        }
    }
    const workspaceSetting = {
        cwd: getCwd(config, workspace),
        workspace: workspace.uri.toString(),
        args: resolveVariables(config.get(`args`) ?? [], workspace),
        path: resolveVariables(config.get(`path`) ?? [], workspace),
        interpreter: resolveVariables(interpreter, workspace),
        importStrategy: config.get(`importStrategy`) ?? 'useBundled',
        showNotifications: config.get(`showNotifications`) ?? 'off',
    };
    return workspaceSetting;
}
function getGlobalValue(config, key, defaultValue) {
    const inspect = config.inspect(key);
    return inspect?.globalValue ?? inspect?.defaultValue ?? defaultValue;
}
async function getGlobalSettings(namespace, includeInterpreter) {
    const config = (0, vscodeapi_1.getConfiguration)(namespace);
    let interpreter = [];
    if (includeInterpreter) {
        interpreter = getGlobalValue(config, 'interpreter', []);
        if (interpreter === undefined || interpreter.length === 0) {
            interpreter = (await (0, python_1.getInterpreterDetails)()).path ?? [];
        }
    }
    const setting = {
        cwd: process.cwd(),
        workspace: process.cwd(),
        args: getGlobalValue(config, 'args', []),
        path: getGlobalValue(config, 'path', []),
        interpreter: interpreter,
        importStrategy: getGlobalValue(config, 'importStrategy', 'useBundled'),
        showNotifications: getGlobalValue(config, 'showNotifications', 'off'),
    };
    return setting;
}
function checkIfConfigurationChanged(e, namespace) {
    const settings = [
        `${namespace}.args`,
        `${namespace}.cwd`,
        `${namespace}.path`,
        `${namespace}.interpreter`,
        `${namespace}.importStrategy`,
        `${namespace}.showNotifications`,
    ];
    const changed = settings.map((s) => e.affectsConfiguration(s));
    return changed.includes(true);
}
//# sourceMappingURL=settings.js.map