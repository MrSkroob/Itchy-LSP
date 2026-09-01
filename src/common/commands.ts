import * as vscode from 'vscode';
import * as path from 'path';
import { getInterpreterDetails } from './python';
import { spawn } from 'child_process';

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

function resolvePath(value: string, uri: vscode.Uri): string {
    const workspaceFolder = vscode.workspace.getWorkspaceFolder(uri)?.uri.fsPath ?? '';

    const fileDirname = path.dirname(uri.fsPath);

    return value.replace(/\$\{workspaceFolder\}/g, workspaceFolder).replace(/\${fileDirname\}/g, fileDirname);
}

async function runCompileCommand(context: vscode.ExtensionContext, path: string) {
    const interpreter = await getInterpreterDetails();
    const pythonPath = interpreter.path;
    const outputPath = vscode.workspace.getConfiguration('Itchy LSP').get('output', '');

    if (!pythonPath) {
        vscode.window.showErrorMessage(
            'No python installation available. Please check the `interpreter` setting in your configuration.',
        );
        return;
    }

    const compilerPath = vscode.Uri.joinPath(context.extensionUri, 'bundled', 'libs').fsPath;
    const child = spawn(pythonPath[0], ['-m', 'itchy', path, outputPath], { cwd: compilerPath });

    child.stdout.on('data', (data) => {
        console.log('[itchy]', data.toString());
    });

    child.stderr.on('data', (data) => {
        console.error('[itchy]', data.toString());
    });

    child.on('error', (error) => {
        console.error('[itchy]', error);

        vscode.window.showErrorMessage(`Failed to start Itchy: ${error.message}`);
    });

    child.on('close', (code) => {
        console.log(`[itchy] exited with code ${code}`);

        if (code === 0) {
            vscode.window.showInformationMessage('Itchy compilation finished.');
        } else {
            vscode.window.showErrorMessage(`Itchy exited with code ${code}.`);
        }
    });
}

export async function compile() {
    const choice = await vscode.window.showQuickPick([
        {
            label: '$(file-code) Compile File',
            value: 'file',
        },
        {
            label: '$(package) Compile Project',
            value: 'project',
        },
    ]);

    if (!choice) {
        return;
    }

    if (choice.value === 'file') {
        await vscode.commands.executeCommand('Itchy LSP.compileFile');
    } else {
        await vscode.commands.executeCommand('Itchy LSP.compileProject');
    }
}

export async function compileFile(context: vscode.ExtensionContext) {
    const editor = vscode.window.activeTextEditor;

    if (!editor || editor.document.languageId !== 'itchy') {
        return;
    }

    await editor.document.save();

    const file = editor.document.uri.fsPath;

    runCompileCommand(context, file);
}

export async function compileProject(context: vscode.ExtensionContext) {
    const editor = vscode.window.activeTextEditor;
    const uri = editor?.document.uri;

    if (!uri) {
        return;
    }

    // const filepath = resolvePath('${workspaceFolder}', uri);

    // await vscode.window.showInformationMessage(filepath);
    const cwd = resolvePath(vscode.workspace.getConfiguration('Itchy LSP').get('cwd', '${workspaceFolder}'), uri);
    // await vscode.window.showInformationMessage(cwd);
    runCompileCommand(context, cwd);
}
