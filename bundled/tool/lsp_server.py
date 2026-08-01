from __future__ import annotations

import sys
from pathlib import Path


LOG_FILE = Path(__file__).resolve().parent / "logging.txt"
BUNDLED_LIBS = Path(__file__).resolve().parent.parent / "libs"

if str(BUNDLED_LIBS) not in sys.path:
    sys.path.insert(0, str(BUNDLED_LIBS))


import logging
import re
from pygls.lsp.server import LanguageServer
from lsprotocol import types
from itchy.scratch_blocks import SCRATCH_BLOCKS, Event
from itchy.itch_ast import build_ast, Program
from itchy.parser import Parser, ExpectedToken, ParseError
from itchy.tokenizer import Definitions
from itchy.assembler import Assembler, CompilerError

parser = Parser()
server = LanguageServer("example-server", "v0.1")
assembler = Assembler()

cached_ast: Program | None = None

# this maps literal strings for autocomplete to the Definitions regex in the tokenizer
KEYWORD_MAP: dict[str, set[str]] = {
    "Define": {"define"},
    "ElseIf": {"elseif"},
    "Return": {"return"},
    "Shared": {"shared"},
    "Event": {"event"},
    "While": {"while"},
    "Break": {"break"},
    "Else": {"else"},
    "For": {"for"},
    "If": {"if"},
    "In": {"in"},
    "Type": {"var", "list", "bool"},
    "Binop": {"and", "or", "not"}
}



def remove_completion_prefix(
    source: str,
) -> tuple[str, str]:
    match = re.search(
        r"[A-Za-z_][A-Za-z0-9_]*$",
        source,
    )

    if match is None:
        return source, ""

    prefix = match.group(0)
    return source[:match.start()], prefix


def get_defined_events(prefix: str):
    return [types.CompletionItem(label=key, kind=types.CompletionItemKind.Function) 
            for key in SCRATCH_BLOCKS 
            if key.startswith(prefix) and isinstance(SCRATCH_BLOCKS[key], Event)]


def get_defined_variables(prefix: str):
    variables: list[types.CompletionItem] = []
    for _, var_data in assembler.variables.items():
        if not var_data.name.startswith(prefix):
            continue

        if ":" in var_data.name:
            continue

        variables.append(
            types.CompletionItem(label=var_data.name, kind=types.CompletionItemKind.Variable)
        )

    return variables


def get_defined_functions(prefix: str):
    available_functions: list[types.CompletionItem] = [types.CompletionItem(label=key, kind=types.CompletionItemKind.Function) 
                                                       for key in SCRATCH_BLOCKS 
                                                       if key.startswith(prefix) and not isinstance(SCRATCH_BLOCKS[key], Event)]

    for procedure in assembler.procedures:
        # hide function-defined stuff
        if not procedure.startswith(prefix):
            continue

        if ":" in procedure: 
            continue
        
        available_functions.append(
            types.CompletionItem(label=procedure, kind=types.CompletionItemKind.Function)
        )

    return available_functions
    


def completion_items_for_expected(
    expected: set[ExpectedToken],
    prefix: str,
) -> list[types.CompletionItem]:
    items: list[types.CompletionItem] = []

    for expectation in expected:
        token_type = expectation.definition
        path = expectation.path


        if token_type.name in KEYWORD_MAP:
            for keyword in KEYWORD_MAP[token_type.name]:
                if keyword.startswith(prefix):
                    items.append(
                        types.CompletionItem(
                            label=keyword,
                            kind=types.CompletionItemKind.Keyword,
                        )
                    )


        if token_type is Definitions.Symbol:
            if "eventstat" in path:
                items.extend(
                    get_defined_events(prefix)
                )
            if "functioncall" in path:
                items.extend(
                    get_defined_functions(prefix)
                )
            if "varlist1" in path or "var" in path:
                items.extend(
                    get_defined_variables(prefix)
                )

    return items



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
    # current_line = document.lines[params.position.line].strip()
    # text_before_cursor = current_line[:params.position.character]
    pre_source, prefix = remove_completion_prefix(document.source)

    try:
        parsed = parser.read(pre_source)
        tree = build_ast(parsed.tree)
        assembler.emit_program(tree)
    except (ParseError, CompilerError):
        pass

    expected = parser.expected_items

    # parse_result, expected = parser.expected_at_cursor(pre_source)
    # if parse_result is not None:
    #     program = build_ast(parse_result.tree)
    #     try:
    #         assembler.prepare()
    #         assembler.emit_program(program)
    #     except (CompilerError, SyntaxError):
    #         pass

    return completion_items_for_expected(expected, prefix)

    # try:
    #     result = parser.read(document.source)
    #     tree = result.tree
    #     log(f"Source: {document.source!r}")
    # except ParseError:
    #     tree = parser.recovered_tree
    #     log("exists tree: " + str(tree is not None))


    # if tree is not None:        
    #     cached_ast = build_ast(tree)
    #     log("Program length: " + str(len(cached_ast.body)))

    #     pre_source, prefix = remove_completion_prefix(text_before_cursor)

    #     try:
    #         assembler.prepare()
    #         assembler.emit_program(cached_ast)

    #     except (CompilerError, SyntaxError):
    #         pass

    #     available_functions = get_defined_functions(prefix)
        
    #     return available_functions

    # return []


if __name__ == "__main__":
    logging.basicConfig(format="[%(levelname)s]: %(message)s", level=logging.DEBUG, filename=LOG_FILE)
    server.start_io()