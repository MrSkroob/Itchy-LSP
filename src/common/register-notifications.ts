import * as vscode from 'vscode';
import { LanguageClient } from 'vscode-languageclient/node';

export function registerClientNotifications(client: LanguageClient) {
    client.onNotification(
        'itchy/retryTextDocumentCompletion',
        async ({ uri, version }: { uri: string; version: number }) => {
            const editor = vscode.window.activeTextEditor;
            console.log('retry request received');
            if (!editor || editor.document.uri.toString() !== uri || editor.document.version !== version) {
                console.log('rejected retry request');
                return;
            }
            console.log('triggerSuggest sent');
            await vscode.commands.executeCommand('editor.action.triggerSuggest');
        },
    );
}
