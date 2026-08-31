import * as vscode from 'vscode';

const sampleSource = `event event_whenflagclicked() {
    looks_say("Hello Itchy!")
}
`;

function testFilename(value: string) {
    return /[<>:"/\\|?*]/.test(value);
}

async function createTarget(projectUri: vscode.Uri, targetName: string) {
    const targetUri = vscode.Uri.joinPath(projectUri, targetName);

    await vscode.workspace.fs.createDirectory(vscode.Uri.joinPath(targetUri, 'costumes'));

    await vscode.workspace.fs.createDirectory(vscode.Uri.joinPath(targetUri, 'sounds'));

    await vscode.workspace.fs.writeFile(
        vscode.Uri.joinPath(targetUri, `${targetName}.itch`),
        Buffer.from(sampleSource, 'utf8'),
    );
}

export async function createScratchProject() {
    const result = await vscode.window.showOpenDialog({
        canSelectFiles: false,
        canSelectFolders: true,
        canSelectMany: false,
        openLabel: 'Select Project Location',
    });

    if (!result) {
        return;
    }

    if (!result?.length) {
        vscode.window.showErrorMessage('No folder selected.');
        return;
    }

    const name = await vscode.window.showInputBox({
        title: 'Create Scratch Project',
        prompt: 'Project name',
        placeHolder: 'Scratch Project',
        validateInput: (value) => {
            if (!value.trim()) {
                return 'Project name cannot be empty.';
            }

            if (testFilename(value)) {
                return 'Project name contains invalid characters.';
            }

            return undefined;
        },
    });

    if (!name) {
        return;
    }

    const projectUri = vscode.Uri.joinPath(result[0], name);
    await createTarget(projectUri, 'Stage');
    await createTarget(projectUri, 'Sprite1');

    await vscode.commands.executeCommand('vscode.openFolder', projectUri, false);
}

export async function addSprite() {
    const workspaceFolder = vscode.workspace.workspaceFolders?.[0];

    if (!workspaceFolder) {
        vscode.window.showErrorMessage('Open an Itchy project before adding a sprite.');
        return;
    }

    const projectUri = workspaceFolder.uri;

    const spriteName = await vscode.window.showInputBox({
        title: 'Add Sprite',
        prompt: 'Enter a name for the new sprite',

        validateInput: async (value) => {
            const name = value.trim();

            if (!name) {
                return 'Sprite name cannot be empty.';
            }

            if (name.toLowerCase() === 'stage') {
                return "'Stage' is reserved for the Scratch stage.";
            }

            if (testFilename(value)) {
                return 'Sprite name contains invalid characters.';
            }

            const spriteUri = vscode.Uri.joinPath(projectUri, name);

            try {
                await vscode.workspace.fs.stat(spriteUri);
                return `Target '${name}' already exists.`;
            } catch {
                return undefined;
            }
        },
    });

    if (!spriteName) {
        return;
    }

    const name = spriteName.trim();

    try {
        await createTarget(projectUri, name);
        vscode.window.showInformationMessage(`Created sprite '${name}' in '${projectUri.path}'`);
    } catch (error) {
        vscode.window.showErrorMessage(`Failed to create sprite: ${error}`);
    }
}
