import * as vscode from "vscode";


const EXTENSION_ID = "MrSkroob.itchy-project-previewer";


export function getItchyVirtualMachine() {
    const extensionContext = vscode.extensions.getExtension(EXTENSION_ID)
    if (!extensionContext) {
        return;
    } else {
        const vmUri = vscode.Uri.joinPath(extensionContext.extensionUri, "dist", "stage", "vm", "itchyVM");

        return vmUri;
    }
}