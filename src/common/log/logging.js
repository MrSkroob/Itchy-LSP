"use strict";
// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.
Object.defineProperty(exports, "__esModule", { value: true });
exports.registerLogger = registerLogger;
exports.traceLog = traceLog;
exports.traceError = traceError;
exports.traceWarn = traceWarn;
exports.traceInfo = traceInfo;
exports.traceVerbose = traceVerbose;
const util = require("util");
class OutputChannelLogger {
    constructor(channel) {
        this.channel = channel;
    }
    traceLog(...data) {
        this.channel.appendLine(util.format(...data));
    }
    traceError(...data) {
        this.channel.error(util.format(...data));
    }
    traceWarn(...data) {
        this.channel.warn(util.format(...data));
    }
    traceInfo(...data) {
        this.channel.info(util.format(...data));
    }
    traceVerbose(...data) {
        this.channel.debug(util.format(...data));
    }
}
let channel;
function registerLogger(logChannel) {
    channel = new OutputChannelLogger(logChannel);
    return {
        dispose: () => {
            channel = undefined;
        },
    };
}
function traceLog(...args) {
    channel?.traceLog(...args);
}
function traceError(...args) {
    channel?.traceError(...args);
}
function traceWarn(...args) {
    channel?.traceWarn(...args);
}
function traceInfo(...args) {
    channel?.traceInfo(...args);
}
function traceVerbose(...args) {
    channel?.traceVerbose(...args);
}
//# sourceMappingURL=logging.js.map