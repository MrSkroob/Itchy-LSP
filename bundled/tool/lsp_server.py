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
from pygls.workspace.text_document import TextDocument, RE_START_WORD, RE_END_WORD
from pygls.lsp.server import LanguageServer
from lsprotocol import types
from itchy.shared_templates import DATA_TO_VARIABLE_TYPE
from itchy.scratch_blocks import SCRATCH_BLOCKS, Event, Reporter, Field, ReturnType, Menu
from itchy.itch_ast import build_ast_with_semantic_tokens, ASTBuilder, SemanticToken
from itchy.parser import Parser, ExpectedToken, ParseError, ParseResult, ParsedNode
from itchy.tokenizer import Definitions
from itchy.assembler import Assembler, CompilerError, VariableTypes, ProcedureInfo, VariableData
from itchy.dummy_nodes import make_dummy_primary, AGGRESSIVE_STRATEGIES, RECOVERY_STRATEGIES, find_node, find_token, make_wrap


completion_ast = ASTBuilder()
func_signature_ast = ASTBuilder()
# parser that tries not to fail so ast can give syntax highlighting to entire file
semantic_parser = Parser(skip_bad_tokens=True, skip_rules_on_fail=AGGRESSIVE_STRATEGIES)
completions_parser = Parser(skip_bad_tokens=False, skip_rules_on_fail={"primary": make_dummy_primary()})
func_signature_parser = Parser(skip_bad_tokens=False, skip_rules_on_fail={"primary": make_dummy_primary()})

server = LanguageServer("example-server", "v0.1")
assembler = Assembler(is_strict=False)


@dataclass(frozen=True)
class CurrentFunction():
    """
    current_arg is indexed from 1
    """
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

# RE_START_WORD = re.compile(Definitions.Symbol.value)
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


BOOLEAN_COMPLETION = [
    types.CompletionItem(label=i, kind=types.CompletionItemKind.Constant)
    for i in ("true", "false")
]


TYPE_COMPLETION = [
    types.CompletionItem(label=i.value, kind=types.CompletionItemKind.TypeParameter)
    for i in VariableTypes
]


def get_function_info_by_name(assembler_snapshot: AssemblerState, name: str) -> tuple[ProcedureInfo, str] | None:
    function_data = assembler_snapshot.procedures.get(name)
    function_type = "function"
    
    if function_data is None:
        block_data = SCRATCH_BLOCKS.get(name)

        if block_data is None:
            return None

        return_types: set[VariableTypes] = set()

        if isinstance(block_data, Event):
            function_type = "scratch event"
        elif isinstance(block_data, Reporter):
            function_type = "scratch reporter"
            return_types.add(block_data.return_type)
        else:
            function_type = "scratch block"

        arguments = block_data.inputs + block_data.fields
        argument_names: list[str] = []
        argument_types: list[VariableTypes] = []

        for i in arguments:
            match i:
                case Menu():
                    argument_names.append(i.field_name or i.name)
                    argument_types.append(VariableTypes.STRING)
                case ReturnType():
                    argument_names.append(i.name)
                    argument_types.append(
                        DATA_TO_VARIABLE_TYPE[i.return_type]
                    )
                case Field():
                    argument_names.append(i.name)
                    argument_types.append(VariableTypes.STRING)


        function_data = ProcedureInfo(
            name=name,
            prototype_id="",
            proccode="",
            argument_ids=(),
            argument_names=tuple(argument_names),
            argument_defaults=(),
            argument_types=tuple(argument_types),
            return_types=return_types
        )

    return function_data, function_type



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


    def get_defined_functions(self, prefix: str, expected_type: VariableTypes | None=None):
        available_functions: list[types.CompletionItem] = []

        for opcode in SCRATCH_BLOCKS:
            if not opcode.startswith(prefix):
                continue

            block = SCRATCH_BLOCKS[opcode]

            if isinstance(block, Event):
                continue

            if expected_type is not None:
                if not isinstance(block, Reporter):
                    continue
                if block.return_type != expected_type:
                    continue

            available_functions.append(types.CompletionItem(label=opcode, kind=types.CompletionItemKind.Function))

        assembler_snapshot = assembler_snapshots.get(self.uri)
        if assembler_snapshot is None:
            return available_functions

        for procedure in assembler_snapshot.procedures:
            # hide function-defined stuff
            if not procedure.startswith(prefix):
                continue

            if ":" in procedure: 
                continue

            if expected_type:
                if expected_type not in assembler_snapshot.procedures[procedure].return_types:
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

        prefix = prefix.strip('"')

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

        expected_type: VariableTypes | None = None

        if current_function is not None:
            assembler_state = assembler_snapshots.get(self.uri)

            if assembler_state is not None:
                func_name = current_function.name
                log(f"Autocompleting: {func_name}")

                if scratch_block := SCRATCH_BLOCKS.get(func_name):
                    parameters = scratch_block.inputs + scratch_block.fields

                    if len(parameters) > 0:
                        current_parameter = parameters[clamp(current_function.current_arg - 1, len(parameters) - 1, 0)]
                        if isinstance(current_parameter, Field):
                            items.extend([types.CompletionItem(label=f'"{i}"', kind=types.CompletionItemKind.Text) 
                                        for i in current_parameter.expected
                                        if i.startswith(prefix)])
                            expected_type = VariableTypes.STRING
                        elif isinstance(current_parameter, Menu):
                            items.extend([types.CompletionItem(label=f'"{i}"', kind=types.CompletionItemKind.Text) 
                                        for i in current_parameter.expected
                                        if i.startswith(prefix)])
                            expected_type = VariableTypes.STRING
                        else:
                            expected_type = DATA_TO_VARIABLE_TYPE[current_parameter.return_type]

                        log(f"Current parameter: {current_parameter.name} for {prefix}")

                        match current_parameter.name:
                            case "LIST":
                                items.extend(self.get_defined_variables(prefix, scope, True))
                            case "VARIABLE":
                                items.extend(self.get_defined_variables(prefix, scope, False))
                            case "BROADCAST_INPUT" | "BROADCAST_OPTION":
                                items.extend(self.get_messages(prefix))
                                expected_type = VariableTypes.STRING
                            case _:
                                pass

                        # if should_skip:
                            # return items
                elif proc_info := assembler_state.procedures.get(func_name):
                    # don't suggest more 
                    if len(proc_info.argument_names) <= current_function.current_arg:
                        return items
                    arg_type = proc_info.argument_types[max(current_function.current_arg - 1, 0)]
                    match arg_type:
                        case VariableTypes.BOOL:
                            items.extend(BOOLEAN_COMPLETION)
                            expected_type = VariableTypes.BOOL
                        case VariableTypes.LIST:
                            items.extend(self.get_defined_variables(prefix, scope, True))
                        case VariableTypes.NUMBER:
                            expected_type = VariableTypes.NUMBER
                        case VariableTypes.STRING:
                            expected_type = VariableTypes.STRING
                        case VariableTypes.VAR:
                            items.extend(self.get_defined_variables(prefix, scope, False))
                        case _:
                            pass
                    # return items
                

        current_function = None

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
                        self.get_defined_functions(prefix, expected_type)
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

    
@server.feature(types.TEXT_DOCUMENT_COMPLETION, types.CompletionOptions(
    trigger_characters=["(", ","]
))
def completions(params: types.CompletionParams) -> list[types.CompletionItem]:
    document = server.workspace.get_text_document(params.text_document.uri)
    autocomplete = Autocomplete(params.text_document.uri)
    prefix, source = remove_completion_prefix(document.lines, params.position)

    parsed = None
    current_function = None
    try:
        parsed = completions_parser.read(source)
        completion_ast.build(parsed.tree)
    except ParseError:
        current_function = get_editing_parameter(completions_parser, completion_ast)

    expected = completions_parser.expected_items
    return autocomplete.completion_items_for_expected(expected, prefix, current_function, completion_ast.function_scope)


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
        variables={i: assembler.variables[assembler.variable_map[i]] for i in assembler.variable_map if assembler.variable_map[i] in assembler.variables},
        procedures=assembler.procedures,
        messages=assembler.messages
    )

    if tree is None:
        return types.SemanticTokens(data=[])
    
    
    tokens = tree[1]

    semantic_tokens = encode_semantic_tokens(tokens)

    return types.SemanticTokens(
        data=semantic_tokens
    )


def get_editing_parameter(parser: Parser, ast_builder: ASTBuilder):
    function_name = None
    active_parameter = None
    parsed = parser.deepest_partial

    if parsed is not None:
        assert isinstance(parsed.tree, ParsedNode)
        try:
            if (node := find_node(parsed.tree, "functioncall")) is not None:
                log("functioncall")

                tree = ast_builder.build_functioncall(node)
                function_name = tree.callee
                active_parameter = len(tree.args)

                if find_token(node, Definitions.CloseBracket):
                    return None
            elif (node := find_node(parsed.tree, "eventstat")) is not None:

                wrap_node = ParsedNode(
                    "wrap",
                    children=make_wrap()
                )
                new_children = node.children + (wrap_node, )
                new_node = ParsedNode(
                    node.name,
                    new_children
                )

                tree = ast_builder.build_eventstat(new_node)

                function_name = tree.name
                active_parameter = len(tree.params)

                # tells us to stop typehinting at the end of the function
                if find_token(node, Definitions.CloseBracket):
                    return None

                log(f"{function_name}: {active_parameter}")

        except ValueError as error:
            log(f"failed to get function info: {str(error)}")

    if not function_name:
        return None

    if not active_parameter:
        return None

    return CurrentFunction(function_name, active_parameter)


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
    current_function = None
    try:
        parsed = func_signature_parser.read(source + prefix)
        func_signature_ast.build(parsed.tree)

        # if func_signature_ast.called_function is not None:
        #     function_name = func_signature_ast.called_function.callee
        #     active_parameter = len(func_signature_ast.called_function.args)

    except ParseError:
        current_function = get_editing_parameter(func_signature_parser, func_signature_ast)


    if current_function is None:
        return

    function_name = current_function.name
    active_parameter = current_function.current_arg

    function_data = get_function_info_by_name(assembler_snapshot, function_name)

    if function_data is None:
        return

    argument_count = len(function_data[0].argument_types)
    argument_labels = [f"{function_data[0].argument_names[i]}: {function_data[0].argument_types[i].value}" for i in range(argument_count)]
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


def symbol_at_position(
    document: TextDocument,
    position: types.Position,
) -> tuple[str, types.Range] | tuple[None, None]:
    """
    almost identical to how `document.word_at_position` works, but also returns the range (if applicable)
    """
    line = document.lines[position.line]

    before = line[:position.character]
    after = line[position.character:]

    left = re.search(RE_START_WORD, before)
    right = re.match(RE_END_WORD, after)

    if left is None or right is None:
        return (None, None)

    left_text = left.group()
    right_text = right.group()
    symbol = left_text + right_text

    if not symbol:
        return (None, None)

    start = position.character - len(left_text)
    end = position.character + len(right_text)

    return symbol, types.Range(
        start=types.Position(position.line, start),
        end=types.Position(position.line, end),
    )


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(params: types.HoverParams) -> types.Hover | None:
    uri = params.text_document.uri


    assembler_state = assembler_snapshots.get(uri)
    if assembler_state is None:
        return

    document = server.workspace.get_text_document(uri)
    word, hover_range = symbol_at_position(document, params.position)

    if word is None:
        return

    if message := assembler_state.messages.get(word):
        contents = types.MarkupContent(
            kind=types.MarkupKind.PlainText,
            value=f"(message name) {message}"
        )
    elif variable := assembler_state.variables.get(word):
        signature = f"(variable) {variable.name}: {variable.var_type.value}"
        contents = types.MarkupContent(
            kind=types.MarkupKind.Markdown,
            value=f"```itchy\n{signature}\n```"
        )
    elif proc_info := get_function_info_by_name(assembler_state, word):
        proc_type = proc_info[1]
        proc_data = proc_info[0]
        if len(proc_data.return_types) > 0:
            return_type = " | ".join([i.value for i in proc_data.return_types])
        else:
            return_type = "nothing"
        signature = f"({proc_type}) {proc_data.name}({", ".join(f"{proc_data.argument_names[i]}: {proc_data.argument_types[i].value}" 
                                                              for i in 
                                                              range(len(proc_data.argument_names)))}) -> {return_type}"
        contents = types.MarkupContent(
            kind=types.MarkupKind.Markdown,
            value=f"```itchy\n{signature}\n```"
        )
    else:
        return

    return types.Hover(
        contents=contents,
        range=hover_range
    )

    
if __name__ == "__main__":
    logging.basicConfig(format="[%(levelname)s]: %(message)s", level=logging.DEBUG, filename=LOG_FILE)
    server.start_io()
