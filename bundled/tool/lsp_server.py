from __future__ import annotations

# from dataclasses import dataclass
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
from itchy.itch_ast import build_ast_with_semantic_tokens, build_ast, get_semantic_tokens, SemanticToken
from itchy.parser import Parser, ExpectedToken, ParseError
from itchy.tokenizer import Definitions
from itchy.assembler import Assembler, CompilerError, VariableTypes

# parser that tries not to fail so ast can give syntax highlighting to entire file
persistent_parser = Parser(skip_bad_tokens=True)
parser = Parser()
server = LanguageServer("example-server", "v0.1")
assembler = Assembler()

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


TYPE_COMPLETION = [
    types.CompletionItem(label=i.value, kind=types.CompletionItemKind.TypeParameter)
    for i in VariableTypes
]


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


def remove_duplicates(items: list[types.CompletionItem]):
    seen: set[str] = set()
    unique: list[types.CompletionItem] = []

    for item in items:
        if item.label in seen:
            continue

        seen.add(item.label)
        unique.append(item)

    return unique
        


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


        if token_type == Definitions.Symbol:
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
            if "equation" in path:
                items.extend(
                    get_defined_variables(prefix)
                )

        if token_type == Definitions.Colon:
            if "argtype" in path:
                items.extend(TYPE_COMPLETION)

    return remove_duplicates(items)



def log(message: str):
    logging.info(message)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def on_save(params: types.DidSaveTextDocumentParams):
    assembler.prepare()
    

@server.feature(types.TEXT_DOCUMENT_COMPLETION, types.CompletionOptions(trigger_characters=(" ")))
def completions(params: types.CompletionParams) -> list[types.CompletionItem]:
    document = server.workspace.get_text_document(params.text_document.uri)
    # current_line = document.lines[params.position.line].strip()
    # text_before_cursor = current_line[:params.position.character]
    pre_source, prefix = remove_completion_prefix(document.source)

    parsed = None
    try:
        parsed = parser.read(pre_source)
        ast = build_ast(parsed.tree)
        assert ast is not None
        assembler.emit_program(ast)
    except (ParseError, CompilerError):
        pass

    expected = parser.expected_items
    return completion_items_for_expected(expected, prefix)



TOKEN_TYPES = {
    "keyword": types.SemanticTokenTypes.Keyword,
    "type": types.SemanticTokenTypes.Type,
    "variable": types.SemanticTokenTypes.Variable,
    "parameter": types.SemanticTokenTypes.Parameter,
    "function": types.SemanticTokenTypes.Function,
    "event": types.SemanticTokenTypes.Event,
    "number": types.SemanticTokenTypes.Number,
    "string": types.SemanticTokenTypes.String,
    "boolean": types.SemanticTokenTypes.Keyword,
    "operator": types.SemanticTokenTypes.Operator,
}


TOKEN_MODIFIERS = {
    "declaration": types.SemanticTokenModifiers.Declaration,
    "readonly": types.SemanticTokenModifiers.Readonly,
    "modification": types.SemanticTokenModifiers.Modification,
}


TOKEN_TYPE_LEGEND = list(dict.fromkeys(TOKEN_TYPES.values()))
TOKEN_MODIFIER_LEGEND = list(TOKEN_MODIFIERS.values())


LEGEND = types.SemanticTokensLegend(
    token_types=TOKEN_TYPE_LEGEND,
    token_modifiers=TOKEN_MODIFIER_LEGEND,
)


TOKEN_TYPE_INDEX = {
    token_type: index
    for index, token_type in enumerate(TOKEN_TYPE_LEGEND)
}


TOKEN_MODIFIER_INDEX = {
    modifier: index
    for index, modifier in enumerate(TOKEN_MODIFIER_LEGEND)
}


def encode_semantic_tokens(
    tokens: list[SemanticToken],
) -> list[int]:
    ordered_tokens = sorted(
        tokens,
        key=lambda token: (
            token.line,
            token.character,
        ),
    )

    data: list[int] = []

    previous_line = 0
    previous_character = 0

    for token in ordered_tokens:
        try:
            token_type_index = TOKEN_TYPE_INDEX[types.SemanticTokenTypes(token.token_type)]
        except KeyError as error:
            raise ValueError(
                f"Unknown semantic token type: {token.token_type!r}"
            ) from error

        modifier_bitset = 0

        for modifier in token.modifiers:
            try:
                modifier_index = TOKEN_MODIFIER_INDEX[types.SemanticTokenModifiers(modifier)]
            except KeyError as error:
                raise ValueError(
                    f"Unknown semantic token modifier: {modifier!r}"
                ) from error

            modifier_bitset |= 1 << modifier_index

        delta_line = token.line - previous_line

        if delta_line == 0:
            delta_character = (
                token.character - previous_character
            )
        else:
            delta_character = token.character

        data.extend(
            [
                delta_line,
                delta_character,
                token.length,
                token_type_index,
                modifier_bitset,
            ]
        )

        previous_line = token.line
        previous_character = token.character

    return data


@server.feature(types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
                types.SemanticTokensRegistrationOptions(
                    legend=LEGEND,
                    full=True,
                    range=False
                    )
                )
def semantic_tokens(params: types.SemanticTokensParams) -> types.SemanticTokens:
    # tree = cached_ast
    document = server.workspace.get_text_document(params.text_document.uri)
    # current_line = document.lines[params.position.line].strip()
    # text_before_cursor = current_line[:params.position.character]

    tree = None

    try:
        parsed = persistent_parser.read(document.source)
        tree = build_ast_with_semantic_tokens(parsed.tree)
    except ParseError as e:
        # pass
        if e.previous_valid_tree is not None:
            tree = build_ast_with_semantic_tokens(e.previous_valid_tree.tree) or get_semantic_tokens()

    if tree is None:
        return types.SemanticTokens(data=[])
    
    
    tokens = tree[1]

    semantic_tokens = encode_semantic_tokens(tokens)

    return types.SemanticTokens(
        data=semantic_tokens
    )


if __name__ == "__main__":
    logging.basicConfig(format="[%(levelname)s]: %(message)s", level=logging.DEBUG, filename=LOG_FILE)
    server.start_io()