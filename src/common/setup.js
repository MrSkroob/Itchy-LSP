"use strict";
// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
Object.defineProperty(exports, "__esModule", { value: true });
exports.loadServerDefaults = loadServerDefaults;
const path = require("path");
const fs = require("fs-extra");
const constants_1 = require("./constants");
function loadServerDefaults() {
    const packageJson = path.join(constants_1.EXTENSION_ROOT_DIR, 'package.json');
    const content = fs.readFileSync(packageJson).toString();
    const config = JSON.parse(content);
    return config.serverInfo;
}
//# sourceMappingURL=setup.js.map