from __future__ import annotations

import sys
from pathlib import Path


LOG_FILE = Path(__file__).resolve().parent / "logging.txt"
BUNDLED_LIBS = Path(__file__).resolve().parent.parent / "libs"

if str(BUNDLED_LIBS) not in sys.path:
    sys.path.insert(0, str(BUNDLED_LIBS))


import logging
from pygls.lsp.server import LanguageServer
from lsprotocol import types
from itchy.scratch_blocks import SCRATCH_BLOCKS 
from itchy.itch_ast import build_ast, Program
from itchy.parser import Parser, ParseError
from itchy.assembler import Assembler, CompilerError, SyntaxError

parser = Parser()
server = LanguageServer("example-server", "v0.1")
assembler = Assembler()

cached_ast: Program | None = None

def get_opcode_options(text: str) -> str:
    index = len(text)

    while index > 0:
        char = text[index - 1]
        if not (char.isalnum() or char == "_"):
            break

        index -= 1

    return text[index:]


def log(message: str):
    logging.info(message)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def on_save(params: types.DidSaveTextDocumentParams):
    assembler.prepare()
    

@server.feature(
    types.TEXT_DOCUMENT_COMPLETION,
)
def completions(params: types.CompletionParams) -> list[types.CompletionItem]:
    global cached_ast

    document = server.workspace.get_text_document(params.text_document.uri)
    current_line = document.lines[params.position.line].strip()
    text_before_cursor = current_line[:params.position.character]

    prefix = get_opcode_options(text_before_cursor)

    try:
        result = parser.read(document.source)
        tree = result.tree
        log(f"Source: {document.source!r}")
    except ParseError as e:
        tree = e.previous_valid_tree.tree if e.previous_valid_tree is not None else None
        log("exists tree: " + str(tree is not None))


    if tree is not None:        
        cached_ast = build_ast(tree)
        available_functions: list[types.CompletionItem] = [types.CompletionItem(label=key, kind=types.CompletionItemKind.Function) for key in SCRATCH_BLOCKS if key.startswith(prefix)]

        log("Program length: " + str(len(cached_ast.body)))

        try:
            assembler.prepare()
            assembler.emit_program(cached_ast)

        except (CompilerError, SyntaxError):
            pass

        for procedure in assembler.procedures:
            # hide function-defined stuff
            if ":" in procedure: 
                continue
            available_functions.append(
                types.CompletionItem(label=procedure, kind=types.CompletionItemKind.Function)
            )

        
        return available_functions

    return []
    
    # if not current_line.endswith("hello."):
    #     return []

    # return [
    #     types.CompletionItem(label="world"),
    #     types.CompletionItem(label="friend"),
    # ]


if __name__ == "__main__":
    logging.basicConfig(format="[%(levelname)s]: %(message)s", level=logging.DEBUG, filename=LOG_FILE)
    server.start_io()