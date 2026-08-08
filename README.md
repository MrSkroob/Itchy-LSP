# The official LSP for the Itchy Language!
Currently supports:
- Basic context aware autocomplete
- variable names, function names, scratch block opcodes
- decent syntax highlighting
- method signature help

<img width="529" height="370" alt="image" src="https://github.com/user-attachments/assets/c38e8004-d21a-43e9-9b5c-1965620e8638" />

..that's it!

# Getting started
Assuming you have VSCode and Python 3.10+ installed:

Install the package:
<img width="1846" height="870" alt="image" src="https://github.com/user-attachments/assets/8133de43-3e37-404e-938e-4792ab3e568d" /> <br />
<img width="1383" height="493" alt="image" src="https://github.com/user-attachments/assets/1a551d4d-1b70-4ee3-b2e2-55e25b54f037" /> <br />
and save wherever you want to. <br />
<img width="360" height="397" alt="image" src="https://github.com/user-attachments/assets/7892ffaa-8114-40fc-9cd9-414704f5575a" /> <br />
<img width="587" height="378" alt="image" src="https://github.com/user-attachments/assets/817d3c12-0c09-4c8e-9bc4-5292e5c093b7" />

# To compile the extension yourself:
(mostly copied from https://github.com/microsoft/vscode-python-tools-extension-template)
## Requirements:
1. VS Code 1.64.0 or greater
2. Python 3.10 or greater
3. node >= 18.17.0
4. npm >= 8.19.0 (npm is installed with node, check npm version, use `npm install -g npm@8.3.0` to update)
5. Python extension for VS Code
You should know to create and work with python virtual environments.

## Steps:
1.Create and activate a python virtual environment for this project in a terminal. Be sure to use the minimum version of python for your tool. This template was written to work with python 3.10 or greater.
2. Install nox in the activated environment: `python -m pip install nox`.
3. Add your favorite tool to requirements.in
4. Run `nox --session setup`.
5. Optional Install test dependencies `python -m pip install -r src/test/python_tests/requirements.txt`. You will have to install these to run tests from the Test Explorer.
6. Open `package.json`, look for and update the following things:
7. Find and replace `<pytool-module>` with module name for your tool. This will be used internally to create settings namespace, register commands, etc. Recommendation is to use lower case version of the name, no spaces, - are ok. For example, replacing `<pytool-module>` 8. with pylint will lead to settings looking like `pylint.args`. Another example, replacing `<pytool-module>` with black-formatter will make settings look like `black-formatter.args`.
9. Find and replace `<pytool-display-name>` with display name for your tool. This is used as the title for the extension in market place, extensions view, output logs, etc. For example, for the black extension this is Black Formatter.
10. Install node packages using `npm install`.
11. Install https://github.com/MrSkroob/Itchy under `bundled/libs`. You can run `pip install -e --upgrade --target bundled/libs <PATH-TO-ITCHY>` if you've cloned the repository.  
12. Run `vsce package`
13. Install the `.vsce` package into VSCode and start using it.
