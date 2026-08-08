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
from dataclasses import dataclass
from typing import Sequence
from pygls.lsp.server import LanguageServer
from lsprotocol import types
from itchy.shared_templates import DATA_TO_VARIABLE_TYPE
from itchy.scratch_blocks import SCRATCH_BLOCKS, Event, Field, ReturnType, Menu
from itchy.itch_ast import build_ast_with_semantic_tokens, ASTBuilder, SemanticToken
from itchy.parser import Parser, ExpectedToken, ParseError, ParseResult, ParsedNode
from itchy.tokenizer import Definitions
from itchy.assembler import Assembler, CompilerError, VariableTypes, ProcedureInfo, VariableData
from itchy.dummy_nodes import make_dummy_primary, RECOVERY_STRATEGIES


completion_ast = ASTBuilder()
func_signature_ast = ASTBuilder()
# parser that tries not to fail so ast can give syntax highlighting to entire file
semantic_parser = Parser(skip_bad_tokens=True, skip_rules_on_fail=RECOVERY_STRATEGIES)
completions_parser = Parser(skip_bad_tokens=False)
func_signature_parser = Parser(skip_bad_tokens=False, skip_rules_on_fail={"primary": make_dummy_primary()})

server = LanguageServer("example-server", "v0.1")
assembler = Assembler()


@dataclass(frozen=True)
class CurrentFunction():
    name: str
    current_arg: int


@dataclass(frozen=True)
class AssemblerState():
    variables: dict[str, VariableData]
    procedures: dict[str, ProcedureInfo]
    messages: dict[str, str]


assembler_snapshots: dict[str, AssemblerState] = {

}


def clamp(a: int, upper_bound: int, lower_bound: int):
    return min(max(a, lower_bound), upper_bound)


WORD_CHARS = re.compile(r'[A-Za-z0-9_]*$')

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
    "Binop": {"and", "or", "not"}
}


TYPE_COMPLETION = [
    types.CompletionItem(label=i.value, kind=types.CompletionItemKind.TypeParameter)
    for i in VariableTypes
]


def remove_completion_prefix(lines: Sequence[str], position: types.Position) -> tuple[str, str]:
    """
    returns prefix - used for filtering suggestions
    rest - everything else. used for parsing
    """
    line = lines[position.line] if position.line < len(lines) else ""
    char = position.character

    before_cursor = line[:char]

    match = WORD_CHARS.search(before_cursor)
    prefix = match.group(0) if match else ""
    word_start = char - len(prefix)

    rest = (
        "".join(lines[:position.line])
        + line[:word_start]
    )

    return prefix, rest



class Autocomplete():
    def __init__(self, document_uri: str):
        self.uri = document_uri


    def get_defined_events(self, prefix: str):
        return [types.CompletionItem(label=key, kind=types.CompletionItemKind.Function) 
                for key in SCRATCH_BLOCKS 
                if key.startswith(prefix) and isinstance(SCRATCH_BLOCKS[key], Event)]


    def get_defined_variables(self, prefix: str, scope: str | None, is_list: bool | None=None):
        variables: list[types.CompletionItem] = []

        assembler_snapshot = assembler_snapshots.get(self.uri)
        if assembler_snapshot is None:
            return variables

        for _, var_data in assembler_snapshot.variables.items():
            if not var_data.name.startswith(prefix):
                continue

            if ":" in var_data.name:
                continue

            if var_data.context != scope:
                continue

            if is_list is not None and var_data.is_list != is_list:
                continue

            variables.append(
                types.CompletionItem(label=var_data.name, kind=types.CompletionItemKind.Variable)
            )

        return variables


    def get_defined_functions(self, prefix: str):
        available_functions: list[types.CompletionItem] = [types.CompletionItem(label=key, kind=types.CompletionItemKind.Function) 
                                                        for key in SCRATCH_BLOCKS 
                                                        if key.startswith(prefix) and not isinstance(SCRATCH_BLOCKS[key], Event)]

        assembler_snapshot = assembler_snapshots.get(self.uri)
        if assembler_snapshot is None:
            return available_functions

        for procedure in assembler_snapshot.procedures:
            # hide function-defined stuff
            if not procedure.startswith(prefix):
                continue

            if ":" in procedure: 
                continue
            
            available_functions.append(
                types.CompletionItem(label=procedure, kind=types.CompletionItemKind.Function)
            )

        return available_functions


    def get_messages(self, prefix: str):
        messages: list[types.CompletionItem] = []
        
        assembler_snapshot = assembler_snapshots.get(self.uri)
        if assembler_snapshot is None:
            return messages

        for message in assembler_snapshot.messages:
            if not message.startswith(prefix):
                continue

            if ":" in message:
                continue

            messages.append(
                types.CompletionItem(label=f'"{message}"', kind=types.CompletionItemKind.Text)
            )

        return messages


    def remove_duplicates(self, items: list[types.CompletionItem]):
        seen: set[str] = set()
        unique: list[types.CompletionItem] = []

        for item in items:
            if item.label in seen:
                continue

            seen.add(item.label)
            unique.append(item)

        return unique
            

    def completion_items_for_expected(
        self, 
        expected: set[ExpectedToken],
        prefix: str,
        current_function: CurrentFunction | None,
        scope: str | None
    ) -> list[types.CompletionItem]:
        items: list[types.CompletionItem] = []

        skip_suggestions = False

        if current_function is not None:
            assembler_state = assembler_snapshots.get(self.uri)

            if assembler_state is not None:
                func_name = current_function.name
                scratch_block = SCRATCH_BLOCKS.get(func_name)

                if scratch_block:
                    parameters = scratch_block.inputs + scratch_block.fields

                    if len(parameters) > 0:
                        current_parameter = parameters[clamp(current_function.current_arg, len(parameters) - 1, 0)]
                        skip_suggestions = True
                        if isinstance(current_parameter, Field):
                            items.extend([types.CompletionItem(label=f'"{i}"', kind=types.CompletionItemKind.Text) 
                                        for i in current_parameter.expected
                                        if i.startswith(prefix)])
                            match current_parameter.name:
                                case "LIST":
                                    items.extend(self.get_defined_variables(prefix, scope, True))
                                case "VARIABLE":
                                    items.extend(self.get_defined_variables(prefix, scope, False))
                                case "BROADCAST_INPUT" | "BROADCAST_OPTION":
                                    items.extend(self.get_messages(prefix))
                                case _:
                                    pass
                        elif isinstance(current_parameter, Menu):
                            items.extend([types.CompletionItem(label=f'"{i}"', kind=types.CompletionItemKind.Text) 
                                        for i in current_parameter.expected
                                        if i.startswith(prefix)])

                            
                                
                            items.extend(self.get_messages(prefix))
                        else:
                            skip_suggestions = False

        current_function = None

        if not skip_suggestions:
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
                            self.get_defined_events(prefix)
                        )
                    if "functioncall" in path:
                        items.extend(
                            self.get_defined_functions(prefix)
                        )
                    if "varlist1" in path or "var" in path:
                        items.extend(
                            self.get_defined_variables(prefix, scope)
                        )
                    if "equation" in path:
                        items.extend(
                            self.get_defined_variables(prefix, scope)
                        )

                if token_type == Definitions.Type:
                    items.extend(TYPE_COMPLETION)

        return self.remove_duplicates(items)


def log(message: str):
    logging.info(message)
    

@server.feature(types.TEXT_DOCUMENT_COMPLETION)
def completions(params: types.CompletionParams) -> list[types.CompletionItem]:
    document = server.workspace.get_text_document(params.text_document.uri)
    autocomplete = Autocomplete(params.text_document.uri)
    prefix, source = remove_completion_prefix(document.lines, params.position)

    parsed = None
    try:
        parsed = completions_parser.read(source)
        completion_ast.build(parsed.tree)
        # moved to syntax highlighting because it uses a parser
        # that doesn't die immediately after the cursor.
        # this ensures that all available functions can be filled in.
        # assembler.prepare()
        # assembler.emit_program(cached_ast)
    except ParseError:
        pass

    expected = completions_parser.expected_items
    return autocomplete.completion_items_for_expected(expected, prefix, None, completion_ast.function_scope)


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


@server.thread()
@server.feature(types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
                types.SemanticTokensRegistrationOptions(
                    legend=LEGEND,
                    full=True,
                    range=False
                    )
                )
def semantic_tokens(params: types.SemanticTokensParams) -> types.SemanticTokens:
    document = server.workspace.get_text_document(params.text_document.uri)
    tree = None

    try:
        semantic_parser.cancel()
        parsed = semantic_parser.read(document.source)
        tree = build_ast_with_semantic_tokens(parsed.tree)
        assembler.prepare()
        assembler.emit_program(tree[0])
    except (ParseError, CompilerError):
        pass
        # if isinstance(e, ParseError):
        #     if e.previous_valid_tree is not None:
        #         tree = build_ast_with_semantic_tokens(e.previous_valid_tree.tree) or get_semantic_tokens()
    
    assembler_snapshots[params.text_document.uri] = AssemblerState(
        assembler.variables,
        assembler.procedures,
        assembler.messages
    )

    if tree is None:
        return types.SemanticTokens(data=[])
    
    
    tokens = tree[1]

    semantic_tokens = encode_semantic_tokens(tokens)

    return types.SemanticTokens(
        data=semantic_tokens
    )


@server.feature(types.TEXT_DOCUMENT_SIGNATURE_HELP,
                types.SignatureHelpOptions(
                    trigger_characters=("(", ","),
                    retrigger_characters=(",")
                ))
def signature_help(params: types.SignatureHelpParams) -> types.SignatureHelp | None:
    document = server.workspace.get_text_document(params.text_document.uri)
    prefix, source = remove_completion_prefix(document.lines, params.position)

    assembler_snapshot = assembler_snapshots.get(params.text_document.uri)
    if assembler_snapshot is None:
        return None

    parsed: ParseResult | None = None
    active_parameter = 0
    function_name = None
    try:
        parsed = func_signature_parser.read(source + prefix)
        func_signature_ast.build(parsed.tree)

        if func_signature_ast.called_function is not None:
            function_name = func_signature_ast.called_function.callee
            active_parameter = len(func_signature_ast.called_function.args)

    except ParseError:
        parsed = func_signature_parser.deepest_partial
        if parsed is not None:
            assert isinstance(parsed.tree, ParsedNode)
            try:
                tree = func_signature_ast.build_functioncall(parsed.tree)
                function_name = tree.callee
                active_parameter = len(tree.args)
            except ValueError:
                pass


    if function_name is None:
        return None
    

    log(f"{function_name}: {active_parameter}")

    function_data = assembler_snapshot.procedures.get(function_name)

    if function_data is None:
        block_data = SCRATCH_BLOCKS.get(function_name)

        if block_data is None:
            return None

        arguments = block_data.inputs + block_data.fields
        argument_names: list[str] = []
        argument_types: list[VariableTypes] = []

        for i in arguments:
            if isinstance(i, Menu):
                argument_names.append(i.field_name or i.name)
            elif isinstance(i, ReturnType):
                argument_names.append(i.name)
                argument_types.append(
                    DATA_TO_VARIABLE_TYPE[i.return_type]
                )

        function_data = ProcedureInfo(
            name=function_name,
            prototype_id="",
            proccode="",
            argument_ids=(),
            argument_names=tuple(argument_names),
            argument_defaults=(),
            argument_types=tuple(argument_types)
        )

    argument_count = len(function_data.argument_types)
    argument_labels = [f"{function_data.argument_names[i]}: {function_data.argument_types[i].value}" for i in range(argument_count)]
    function_label = f"{function_name}({', '.join(argument_labels)})"

    return types.SignatureHelp(
        signatures=[
            types.SignatureInformation(
                label=function_label,
                parameters=[
                    types.ParameterInformation(
                        label=i,
                    )
                    for i in argument_labels
                ]
            ),
        ],
        active_signature=0,
        active_parameter=max(active_parameter - 1, 0)
    )

    
    
if __name__ == "__main__":
    logging.basicConfig(format="[%(levelname)s]: %(message)s", level=logging.DEBUG, filename=LOG_FILE)
    server.start_io()
