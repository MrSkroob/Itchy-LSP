from __future__ import annotations

# from dataclasses import dataclass
import sys
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent / "logging.txt"
BUNDLED_LIBS = Path(__file__).resolve().parent.parent / "libs"

if str(BUNDLED_LIBS) not in sys.path:
    sys.path.insert(0, str(BUNDLED_LIBS))


import asyncio
import logging
import re
from enum import Enum
from dataclasses import dataclass, field, replace
from typing import Iterable, Sequence, Callable, TypeVar, Protocol
# from pygls.workspace.text_document import TextDocument, RE_START_WORD, RE_END_WORD
from pygls.uris import to_fs_path
from pygls.lsp.server import LanguageServer
from lsprotocol import types
from itchy.shared_templates import DATA_TO_VARIABLE_TYPE, SourceSpan
from itchy.scratch_blocks import SCRATCH_BLOCKS, STAGE_BLOCKS, Event, Reporter, Field, ReturnType, Menu
from itchy.itch_ast import Expr, build_ast_with_semantic_tokens, utf16_length, ASTBuilder, SemanticToken, FunctionCallStmt, EventHandlerStmt
from itchy.parser import Parser, ExpectedToken, ParseError, ParseResult, ParsedNode
from itchy.tokenizer import Definitions
from itchy.assembler import Assembler, VariableTypes, ProcedureInfo, VariableData, MessageData, CompilerErrorCodes, SymbolOccurence, SymbolType
from itchy.dummy_nodes import make_dummy_primary, ANALYSIS_STRATEGIES, find_nodes, find_last_node, find_token, make_wrap
from itchy.errors import get_message, CompilerError, CompilerWarning


completion_ast = ASTBuilder()
func_signature_ast = ASTBuilder()
# parser that tries not to fail so ast can give syntax highlighting to entire file
semantic_parser = Parser(skip_bad_tokens=True, skip_rules_on_fail=ANALYSIS_STRATEGIES)
completions_parser = Parser(skip_bad_tokens=False, skip_rules_on_fail=ANALYSIS_STRATEGIES)
func_signature_parser = Parser(skip_bad_tokens=False, skip_rules_on_fail=ANALYSIS_STRATEGIES)

analysis_parser = Parser(skip_bad_tokens=True, skip_rules_on_fail={"primary": make_dummy_primary})
analysis_ast = ASTBuilder()
# analysis_assembler = Assembler(is_strict=False)

server = LanguageServer("example-server", "v0.1")

T = TypeVar("T")
# assembler = Assembler(is_strict=False)


class ResponseErrorCodes(Enum):
    FILE_NOT_READY = 1
    SYMBOL_NOT_FOUND = 2


@dataclass(frozen=True)
class CurrentFunction():
    """
    current_arg is indexed from 1
    """
    name: str
    current_arg: int


@dataclass(frozen=True)
class AssemblerState():
    uri: str # is the uri that's used to access files. DIFFERENT to the key of what this is stored in
    symbols: list[SymbolOccurence]
    variables: dict[tuple[str, str | None], VariableData]
    procedures: dict[str, ProcedureInfo]
    messages: dict[str, MessageData]


@dataclass
class TargetFiles:
    sprite_name: str
    costumes: list[str]
    sounds: list[str]


@dataclass()
class Session():
    variables: dict[str, VariableData] = field(default_factory=dict[str, VariableData]) 
    messages: dict[str, MessageData] = field(default_factory=dict[str, MessageData])

session = Session()
file_cache: dict[str, TargetFiles] = {}
assembler_snapshots: dict[str, AssemblerState] = {}


def clamp(a: int, upper_bound: int, lower_bound: int):
    return min(max(a, lower_bound), upper_bound)

# RE_START_WORD = re.compile(Definitions.Symbol.value)
WORD_CHARS = re.compile(r'[A-Za-z0-9_]*$')

# this maps literal strings for autocomplete to the Definitions regex in the tokenizer
KEYWORD_MAP: dict[str, set[str]] = {
    Definitions.Define.name: {"define"},
    Definitions.ElseIf.name: {"elseif"},
    Definitions.Return.name: {"return"},
    Definitions.Shared.name: {"shared"},
    Definitions.Event.name: {"event"},
    Definitions.While.name: {"while"},
    Definitions.Bool.name: {"true", "false"},
    Definitions.Else.name: {"else"},
    Definitions.Warp.name: {"warp"},
    Definitions.For.name: {"for"},
    Definitions.If.name: {"if"},
    Definitions.In.name: {"in"},
    Definitions.Binop.name: {"and", "or", "not"}
}


BOOLEAN_COMPLETION = [
    types.CompletionItem(label=i, kind=types.CompletionItemKind.Constant)
    for i in ("true", "false")
]


TYPE_COMPLETION = [
    types.CompletionItem(label=i.value, kind=types.CompletionItemKind.TypeParameter)
    for i in VariableTypes
]


def get_block_pool(uri: str):
    path = Path(uri_to_fs(uri))
    target = path.stem.casefold()
    if target == "stage":
        return STAGE_BLOCKS
    else:
        return SCRATCH_BLOCKS


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
            return_types = block_data.return_type
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
            return_types=return_types,
            definition_location=None
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
        self.block_pool = get_block_pool(document_uri)
        self.fs_path = uri_to_fs(document_uri)
        log(f"AUTOCOMPLETE PATH: {self.fs_path}")

    def get_defined_sprites(self, prefix: str):
        return [types.CompletionItem(label=f'"{key.sprite_name}"', kind=types.CompletionItemKind.Text)
                for key in file_cache.values()
                if key.sprite_name.startswith(prefix)]

    def get_defined_costumes(self, prefix: str):
        return [types.CompletionItem(label=f'"{key}"', kind=types.CompletionItemKind.Text)
                for key in file_cache.get(self.fs_path, TargetFiles("", [], [])).costumes
                if key.startswith(prefix)]

    def get_defined_sounds(self, prefix: str):
        return [types.CompletionItem(label=f'"{key}"', kind=types.CompletionItemKind.Text)
                for key in file_cache.get(self.fs_path, TargetFiles("", [], [])).sounds
                if key.startswith(prefix)]

    def get_defined_events(self, prefix: str):
        return [types.CompletionItem(label=key, kind=types.CompletionItemKind.Function) 
                for key in self.block_pool 
                if key.startswith(prefix) and isinstance(self.block_pool[key], Event)]

    def get_all_private_variables(self, prefix: str):
        variables: list[types.CompletionItem] = []
        for assembler in assembler_snapshots.values():
            for variable in assembler.variables.values():
                if variable.shared:
                    continue
                if not variable.name.startswith(prefix):
                    continue

                variables.append(
                    types.CompletionItem(label=f'"{variable.name}"', kind=types.CompletionItemKind.Text)
                )
        return variables

    def get_defined_variables(self, prefix: str, scope: str | None, is_list: bool | None=None):
        variables: list[types.CompletionItem] = []

        assembler_snapshot = assembler_snapshots.get(self.fs_path)
        if assembler_snapshot is None:
            return variables

        for _, var_data in assembler_snapshot.variables.items():
            if not var_data.name.startswith(prefix):
                continue

            if ":" in var_data.name:
                continue

            if var_data.context.function_context != scope:
                continue

            if is_list is not None and var_data.is_list != is_list:
                continue

            variables.append(
                types.CompletionItem(label=var_data.name, kind=types.CompletionItemKind.Variable)
            )

        return variables

    def get_defined_functions(self, prefix: str, expected_type: VariableTypes | None=None):
        available_functions: list[types.CompletionItem] = []

        for opcode in self.block_pool:
            if not opcode.startswith(prefix):
                continue

            block = self.block_pool[opcode]

            if isinstance(block, Event):
                continue

            if expected_type is not None:
                if not isinstance(block, Reporter):
                    continue
                if expected_type not in block.return_type:
                    continue

            available_functions.append(types.CompletionItem(label=opcode, kind=types.CompletionItemKind.Function))

        assembler_snapshot = assembler_snapshots.get(self.fs_path)
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
        
        assembler_snapshot = assembler_snapshots.get(self.fs_path)
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
            assembler_state = assembler_snapshots.get(self.fs_path)

            if assembler_state is not None:
                func_name = current_function.name

                if scratch_block := self.block_pool.get(func_name):
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

                        match current_parameter.name:
                            case "LIST":
                                items.extend(self.get_defined_variables(prefix, scope, True))
                            case "VARIABLE":
                                items.extend(self.get_defined_variables(prefix, scope, False))
                            case "BROADCAST_INPUT" | "BROADCAST_OPTION":
                                items.extend(self.get_messages(prefix))
                                expected_type = VariableTypes.STRING
                            case "BACKDROP" | "COSTUME":
                                items.extend(self.get_defined_costumes(prefix))
                                expected_type = VariableTypes.STRING
                            case "SOUND_MENU":
                                items.extend(self.get_defined_sounds(prefix))
                                expected_type = VariableTypes.STRING
                            case "OBJECT": 
                                items.extend(self.get_defined_sprites(prefix))
                                expected_type = VariableTypes.STRING
                            case "PROPERTY":
                                items.extend(self.get_all_private_variables(prefix))
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
    uri = params.text_document.uri
    document = server.workspace.get_text_document(uri)
    autocomplete = Autocomplete(uri)
    prefix, source = remove_completion_prefix(document.lines, params.position)

    parsed = None
    current_function = None
    try:
        completions_parser.cancel()
        parsed = completions_parser.read(source)
        completion_ast.build(parsed.tree)
    except (ParseError, InterruptedError) as e:
        if isinstance(e, InterruptedError):
            return []
        current_function = get_editing_parameter(completions_parser, completion_ast, uri)

    expected = completions_parser.expected_items
    return autocomplete.completion_items_for_expected(expected, prefix.strip(), current_function, completion_ast.function_scope)


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
    tokens: Iterable[SemanticToken],
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


def syntax_highlight_document(uri: str):
    document = server.workspace.get_text_document(uri)
    assembler = Assembler(uri, is_strict=False, compile_with_warnings=True)
    tree = None

    try:
        semantic_parser.cancel()
        parsed = semantic_parser.read(document.source)
        tree = build_ast_with_semantic_tokens(parsed.tree)

        # we populate the assembler with shared variables and messages from other documents.
        assembler.prepare()
        assembler.emit_program(tree[0])
    except (ParseError, CompilerError, InterruptedError) as e:
        if isinstance(e, InterruptedError):
            return types.SemanticTokens(data=[])
    

    if tree is None:
        return None


    # for symbol in assembler.symbols:
    #     tree[1][symbol.span] = SemanticToken(
    #         symbol.span.start.line - 1,
    #         symbol.span.start.character - 1,
    #         utf16_length(symbol.name),
    #         symbol.symbol_type
    #     )

    
    tokens = tree[1]

    semantic_tokens = encode_semantic_tokens(tokens.values())

    return types.SemanticTokens(
        data=semantic_tokens
    )



@server.thread()
@server.feature(types.TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
                types.SemanticTokensRegistrationOptions(
                    legend=LEGEND,
                    full=True,
                    range=False
                    )
                )
def semantic_tokens(params: types.SemanticTokensParams) -> types.SemanticTokens | None:
    return syntax_highlight_document(params.text_document.uri)


def get_function_name(parser: Parser, ast_builder: ASTBuilder):
    function_name = None
    parsed = parser.deepest_partial

    if parsed is not None:
        assert isinstance(parsed.tree, ParsedNode)
        try:
            if (node := find_last_node(parsed.tree, "function")) is not None:
                tree = ast_builder.build_function(node)
                function_name = tree.name

            
        except (ValueError, IndexError) as error:
            log(f"failed to get function info: {str(error)}")

    if not function_name:
        return None

    return function_name


def get_incomplete_node(root_node: ParsedNode, 
                        node_name: str, 
                        statement_seperator: Definitions, 
                        uri: str, 
                        ast_builder: ASTBuilder,
                        incomplete_node_data: Callable[[ASTBuilder, ParsedNode, str], tuple[T, bool] | None]) -> T | None:
    """
    Returns the last incomplete node (if any)
    """
    nodes = find_nodes(root_node, node_name)
    iterations = 0
    while len(nodes) > 0:
        iterations += 1
        node = nodes.pop()
        if node.dummy_node:
            continue


        found_token = find_token(node, statement_seperator)
        non_complete_node = incomplete_node_data(ast_builder, node, uri)
        if non_complete_node is None:
            continue
        if (not found_token or found_token[0].dummy_token
                            or non_complete_node[1]):
            return non_complete_node[0]

    return None


# def is_functionexpr_node_incomplete()


def count_args(args: tuple[Expr, ...]):
    count = 0
    for arg in args:
        if arg.dummy:
            continue
        count += 1
    return count


def is_functioncall_node_incomplete(ast_builder: ASTBuilder, node: ParsedNode, uri: str) -> tuple[FunctionCallStmt, bool] | None:
    assembler_snapshot = assembler_snapshots.get(uri_to_fs(uri))
    if assembler_snapshot is None:
        return None

    try:
        tree = ast_builder.build_functioncall(node)
        current_args = count_args(tree.args)
        func_data = get_function_info_by_name(assembler_snapshot, tree.callee)
        if func_data is None:
            return None
        if current_args < len(func_data[0].argument_names):
            return tree, True
        return tree, False
    except ValueError:
        return None


def is_eventstat_node_incomplete(ast_builder: ASTBuilder, node: ParsedNode, uri: str) -> tuple[EventHandlerStmt, bool] | None:
    assembler_snapshot = assembler_snapshots.get(uri_to_fs(uri))
    if assembler_snapshot is None:
        return None
    
    wrap_node = ParsedNode(
        "wrap",
        children=make_wrap()
    )
    new_children = node.children + (wrap_node, )
    new_node = ParsedNode(
        node.name,
        new_children
    )
    try:
        tree = ast_builder.build_eventstat(new_node)
        current_args = count_args(tree.params)
        event_data = get_function_info_by_name(assembler_snapshot, tree.name)
        if event_data is None:
            return None
        if current_args < len(event_data[0].argument_names):
            return tree, True
        return tree, False
    except ValueError:
        return None


def get_editing_parameter(parser: Parser, ast_builder: ASTBuilder, uri: str):
    function_name = None
    active_parameter = None
    parsed = parser.deepest_partial

    if parsed is not None:
        assert isinstance(parsed.tree, ParsedNode)
        try:
            if (node := get_incomplete_node(parsed.tree,
                                            "functioncall",
                                            Definitions.CloseBracket,
                                            uri,
                                            ast_builder,
                                            is_functioncall_node_incomplete)):
                function_name = node.callee
                active_parameter = len(node.args)
            elif (node := get_incomplete_node(parsed.tree, 
                                              "eventstat", 
                                              Definitions.CloseBracket,
                                              uri,
                                              ast_builder,
                                              is_eventstat_node_incomplete
                                              )) is not None:

                function_name = node.name
                active_parameter = len(node.params)


        except ValueError:
            pass


    if not function_name:
        return

    if not active_parameter:
        return

    return CurrentFunction(function_name, active_parameter)


@server.feature(types.TEXT_DOCUMENT_SIGNATURE_HELP,
                types.SignatureHelpOptions(
                    trigger_characters=("(", ","),
                    retrigger_characters=(",")
                ))
def signature_help(params: types.SignatureHelpParams) -> types.SignatureHelp | None:
    uri = params.text_document.uri
    document = server.workspace.get_text_document(uri)
    _, source = remove_completion_prefix(document.lines, params.position)

    assembler_snapshot = assembler_snapshots.get(uri_to_fs(uri))
    if assembler_snapshot is None:
        return None

    parsed: ParseResult | None = None
    current_function = None
    try:
        parsed = func_signature_parser.read(source)
        func_signature_ast.build(parsed.tree)

        # if func_signature_ast.called_function is not None:
        #     function_name = func_signature_ast.called_function.callee
        #     active_parameter = len(func_signature_ast.called_function.args)

    except ParseError:
        current_function = get_editing_parameter(func_signature_parser, func_signature_ast, uri)


    if current_function is None:
        return

    function_name = current_function.name
    active_parameter = current_function.current_arg

    function_data = get_function_info_by_name(assembler_snapshot, function_name)

    if function_data is None:
        return

    argument_count = len(function_data[0].argument_types)
    offset = 0
    if function_data[1] == "function":
        offset = 1
    argument_labels = [f"{function_data[0].argument_names[i]}: {function_data[0].argument_types[i].value}" for i in range(argument_count - offset)]
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


def position_in_span(
    position: types.Position,
    length: int,
    span: SourceSpan,
) -> bool:
    return (
        span.start.line - 1 == position.line
        and span.start.character - 1 <= position.character < span.start.character + length
    )


def symbol_at_position(
    symbols: list[SymbolOccurence],
    position: types.Position,
) -> SymbolOccurence | None:
    for symbol in symbols:
        if symbol.span.start.line == -1:
            continue
        if position_in_span(position, utf16_length(symbol.name), symbol.span):
            return symbol

    return None


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(params: types.HoverParams) -> types.Hover | None:
    uri = params.text_document.uri


    assembler_state = assembler_snapshots.get(uri_to_fs(uri))
    if assembler_state is None:
        return

    word = symbol_at_position(assembler_state.symbols, params.position)

    if word is None:
        return 

    contents = f"({word.symbol_type}) {word.name}"

    match word.symbol_type:
        case SymbolType.FUNCTION | SymbolType.EVENT:
            proc_info = get_function_info_by_name(assembler_state, word.name)

            if not proc_info:
                return

            proc_type = proc_info[1]
            proc_data = proc_info[0]
            if len(proc_data.return_types) > 0:
                return_type = " | ".join([i.value for i in proc_data.return_types])
            else:
                return_type = "nothing"

            offset = 0
            if proc_type == "function":
                offset = 1

            signature = f"({proc_type}) {proc_data.name}({", ".join(f"{proc_data.argument_names[i]}: {proc_data.argument_types[i].value}" 
                                                                for i in 
                                                                range(len(proc_data.argument_names) - offset))}) -> {return_type}"
            contents = types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value=f"```itchy\n{signature}\n```"
            )
        case SymbolType.VARIABLE:
            variable = assembler_state.variables.get((word.name, None))
            if not variable:
                return
            contents += f": {variable.var_type.value}"
        case SymbolType.PARAMETER:
            variable = assembler_state.variables.get((word.name, word.context))
            if not variable:
                return
            contents += f": {variable.var_type.value}"

    hover_range = span_to_range(word.span)


    return types.Hover(
        contents=contents,
        range=hover_range
    )


def span_to_range(span: SourceSpan) -> types.Range:
    return types.Range(
        start=types.Position(
            line=max(span.start.line - 1, 0),
            character=max(span.start.character - 1, 0)
        ),
        end=types.Position(
            line=max(span.end.line - 1, 0),
            character=max(span.end.character - 1, 0)
        )
    )


@server.feature(types.TEXT_DOCUMENT_CODE_ACTION,
                types.CodeActionOptions(
                    code_action_kinds=[types.CodeActionKind.QuickFix]
                ))
def code_action(params: types.CodeActionParams) -> list[types.CodeAction]:
    actions: list[types.CodeAction] = []
    uri = params.text_document.uri
    document = server.workspace.get_text_document(uri)

    for diagnostic in params.context.diagnostics:
        match diagnostic.code:
            case CompilerErrorCodes.NOTHING_RETURN:
                if diagnostic.data is None:
                    continue

                assembler_state = assembler_snapshots.get(uri_to_fs(uri))
                if assembler_state is None:
                    continue

                proc_info = assembler_state.procedures.get(diagnostic.data["name"])

                if proc_info is None:
                    continue

                if proc_info.last_location is None:
                    continue

                location = proc_info.last_location
                location_range = span_to_range(location)
                line_text = document.lines[location_range.start.line]
                indent = line_text[:len(line_text) - len(line_text.lstrip())]

                edit = types.TextEdit(
                    range=types.Range(
                        start=types.Position(location_range.end.line + 1, 0),
                        end=types.Position(location_range.end.line + 1, 0)
                    ),
                    new_text = f"{indent}return\n"
                )

                actions.append(
                    types.CodeAction(
                        title="Add return statement",
                        kind=types.CodeActionKind.QuickFix,
                        diagnostics=[diagnostic],
                        edit=types.WorkspaceEdit(
                            changes={
                                uri: [edit]
                            }
                        )
                    )
                )
            case CompilerErrorCodes.UNDEFINED_VARIABLE:
                if diagnostic.data is None:
                    continue

                assembler_state = assembler_snapshots.get(uri_to_fs(uri))
                line = 1
                if assembler_state is not None:
                    line = len(document.lines)
                    
                    for key, variable in assembler_state.variables.items():
                        if key[1] is not None:
                            continue
                        if variable.definition_location is None:
                            continue
                        if variable.definition_location.start.line < line:
                            line = variable.definition_location.start.line


                name = diagnostic.data["name"]

                edit = types.TextEdit(
                    range=types.Range(
                        start=types.Position(line - 1, 0),
                        end=types.Position(line - 1, 0)
                    ),
                    new_text = f"var {name}\n"
                )

                actions.append(
                    types.CodeAction(
                        title="Define variable",
                        kind=types.CodeActionKind.QuickFix,
                        diagnostics=[diagnostic],
                        edit=types.WorkspaceEdit(
                            changes={
                                uri: [edit]
                            }
                        )
                    )
                )
            case CompilerErrorCodes.REMOVE_RETURN:
                edit = types.TextEdit(
                    range=diagnostic.range,
                    new_text=""
                )

                actions.append(
                    types.CodeAction(
                        title="Remove return statement",
                        kind=types.CodeActionKind.QuickFix,
                        diagnostics=[diagnostic],
                        edit=types.WorkspaceEdit(
                            changes={
                                uri: [edit],
                            }
                        ),
                    )
                )
            case _:
                pass

    return actions


def lint_document(uri: str):
    """
    Lints a single document. Returns True if the namespace has updated. 
    """
    document = server.workspace.get_text_document(uri)
    assembler = Assembler(uri, is_strict=False, compile_with_warnings=True)
    updated_globals = False

    was_in: set[str] = set()
    was_in_message: set[str] = set()

    for key in list(session.variables):
        var_data = session.variables[key]
        if not compare_uris(var_data.uri, uri):
            continue
        was_in.add(key)
        del session.variables[key]

    for key in list(session.messages):
        message_data = session.messages[key]
        if not compare_uris(message_data.uri, uri):
            continue
        was_in_message.add(key)
        del session.messages[key]

    try:
        analysis_parser.cancel()
        parsed = analysis_parser.read(document.source)
        tree = analysis_ast.build(parsed.tree)
        assembler.prepare(global_messages=session.messages, global_variables=session.variables)
        assembler.emit_program(tree)
    except (ParseError, CompilerError, InterruptedError) as e:
        if isinstance(e, InterruptedError):
            return updated_globals

    variables: dict[tuple[str, str | None], VariableData] = {}
    messages: dict[str, MessageData] = {}
    
    for key, var_id in assembler.variable_map.items():
        variables[key] = assembler.variables[var_id]

    for key, message_data in assembler.messages.items():
        messages[key] = message_data
    
    assembler_snapshots[uri_to_fs(uri)] = AssemblerState(
        uri=uri,
        symbols=assembler.symbols,
        variables=variables,
        procedures=assembler.procedures,
        messages=messages
    )

    for _, message_data in assembler.messages.items():
        if not compare_uris(message_data.uri, uri):
            continue

        if message_data.name not in was_in_message:
            updated_globals = True
        else:
            was_in_message.remove(message_data.name)

        session.messages[message_data.name] = message_data

    for _, var_data in assembler.variables.items():
        if not var_data.shared:
            continue

        if not compare_uris(var_data.uri, uri):
            continue

        if var_data.name not in was_in:
            # variable was added
            updated_globals = True
        else:
            was_in.remove(var_data.name)

        session.variables[var_data.name] = var_data

    if len(was_in) > 0 or len(was_in_message) > 0:
        # variable was deleted
        updated_globals = True


    linting_errors = assembler.errors
    syntax_errors = list(analysis_parser.speculative_errors.values()) + \
        [i for i in analysis_parser.accumulated_errors if i.pos not in analysis_parser.speculative_errors]

    diagnostics: list[types.Diagnostic] = []
    seen: set[tuple[int, int]] = set()

    for error in syntax_errors:
        if len(error.tokens) == 0:
            continue
        if (error.pos, -1) in seen:
            continue
        seen.add((error.pos, -1))
        token = error.tokens[min(len(error.tokens) - 1, error.pos)]
        key = get_message(error, analysis_parser.expected)
        diagnostics.append(
            types.Diagnostic(
                range=span_to_range(token.span),
                message=key,
                severity=types.DiagnosticSeverity.Error,
                data={
                    "token": token
                }
            )
        )

    for error in linting_errors:
        node = error.error_node
        if node is None:
            continue

        if node.span.start.line == -1:
            continue

        if not error.error_node:
            continue
        key = (error.error_node.span.start.line, error.error_node.span.start.character)

        if key in seen:
            continue

        seen.add(key)

        diagnostics.append(
            types.Diagnostic(
                range=span_to_range(node.span),
                message=f"({error.__class__.__name__}) {error.message}",
                severity=types.DiagnosticSeverity.Warning if isinstance(error, CompilerWarning) 
                else types.DiagnosticSeverity.Error,
                code=error.error_code,
                data=error.data
            )
        )

    server.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(
            uri=uri,
            diagnostics=diagnostics
        )
    )

    return updated_globals



# linting_documents: set[str] = set()

def uri_to_fs(uri: str, keep_case: bool=False):
    """
    Returns a filesystem path that's consistent no matter what URI you feed into it.
    If the URI is already an fs path, it will return the fs path.
    """
    path = to_fs_path(uri)

    if path is None:
        return uri.casefold() if not keep_case else uri

    return path.casefold() if not keep_case else path


def compare_uris(a: str, b: str):
    """
    Compares both uris/fs by using `uri_to_fs` on both
    """
    path_a = uri_to_fs(a)
    path_b = uri_to_fs(b)

    return path_a == path_b


def lint_documents_with_changes(uri: str):
    """
    Lints a single document, but will update other documents if the globals have changed.
    """
    globals_changed = lint_document(uri)
    if globals_changed:
        for other_uri in server.workspace.text_documents:
            if compare_uris(other_uri, uri):
                continue
            lint_document(other_uri)


@server.feature(types.WORKSPACE_DID_DELETE_FILES)
def did_delete_files(params: types.DeleteFilesParams):
    session.messages.clear()
    session.variables.clear()

    for file in params.files:
        fs_path = uri_to_fs(file.uri)
        if fs_path in assembler_snapshots:
            del assembler_snapshots[fs_path]


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(params: types.DidCloseTextDocumentParams):
    # clear any diagnostics that we had before
    uri = params.text_document.uri
    

    server.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(
            uri=uri,
            diagnostics=[]
        )
    )


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def on_open(params: types.DidOpenTextDocumentParams):
    syntax_highlight_document(params.text_document.uri)
    lint_documents_with_changes(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
async def linter(params: types.DidChangeTextDocumentParams):
    uri = params.text_document.uri
    version = params.text_document.version

    # Debounce rapid edits so we only lint the latest document state.
    await asyncio.sleep(0.1)

    document = server.workspace.get_text_document(uri)
    if document.version != version:
        return

    lint_documents_with_changes(uri)


@server.feature(types.TEXT_DOCUMENT_DEFINITION)
def goto_definition(params: types.DefinitionParams) -> types.Location | None:
    uri = params.text_document.uri
    assembler_state = assembler_snapshots.get(uri_to_fs(params.text_document.uri))
    if assembler_state is None:
        return

    symbol = symbol_at_position(assembler_state.symbols, params.position)
    if symbol is None:
        return
    
    location = symbol.definition_location

    if location is None:
        for _, state in assembler_snapshots.items():
            key = (symbol.name, None)
            
            variable = state.variables.get(key)
            if variable is None:
                continue

            if variable.definition_location is not None:
                uri = state.uri
                location = variable.definition_location
                break

    if location is None:
        return

    return types.Location(
        uri=uri,
        range=span_to_range(location)
    )


def replace_symbol(fs_path: str, symbol: SymbolOccurence, original: str, replace_with: str) -> list[types.TextEdit]:
    assembler_state = assembler_snapshots.get(fs_path)
    if assembler_state is None:
        return []

    edits: list[types.TextEdit] = []
    
    for other_symbol in assembler_state.symbols:
        if other_symbol.span.start.line == -1:
            continue

        if other_symbol.name != original:
            continue

        if symbol.symbol_type == SymbolType.PARAMETER:
            if symbol.context != other_symbol.context:
                continue
        
        if symbol.symbol_type == other_symbol.symbol_type:
            edits.append(types.TextEdit(
                range=span_to_range(other_symbol.span),
                new_text=replace_with
            ))

    return edits


@server.feature(types.TEXT_DOCUMENT_RENAME)
def rename_symbol(params: types.RenameParams) -> types.WorkspaceEdit | types.ResponseError | None:
    current_uri = params.text_document.uri
    assembler_state = assembler_snapshots.get(uri_to_fs(current_uri))
    if assembler_state is None:
        return types.ResponseError(code=ResponseErrorCodes.FILE_NOT_READY.value, message="File not finished linting. Please try again later.")
    
    symbol = symbol_at_position(assembler_state.symbols, params.position)
    if symbol is None:
        return None

    edits: dict[str, list[types.TextEdit]] = {}

    ignore_other_uris = True
    if symbol.symbol_type == SymbolType.VARIABLE:
        variable = assembler_state.variables.get((symbol.name, symbol.context))
        if variable is None:
            return types.ResponseError(code=ResponseErrorCodes.SYMBOL_NOT_FOUND.value, message="Symbol not found.")

        # only replace variable if it's a shared variable
        if variable.shared:
            ignore_other_uris = False


    for fs_path, assembler_state in assembler_snapshots.items():
        if not compare_uris(fs_path, current_uri) and ignore_other_uris:
            continue
        edits[assembler_state.uri] = replace_symbol(fs_path, symbol, symbol.name, params.new_name)

    lint_documents_with_changes(current_uri)

    return types.WorkspaceEdit(
        changes=edits
    )


class TargetFilesParams(Protocol):
    uri: str
    costumes: list[str]
    sounds: list[str]


class TargetFilesCacheParams(Protocol):
    targets: list[TargetFilesParams]


def update_file_cache(params: TargetFilesParams):
    target_fs_path = to_fs_path(params.uri)
    if target_fs_path is None:
        return

    target_path = Path(target_fs_path)
    document_path = target_path / f"{target_path.name}.itch"
    file_cache[str(document_path).casefold()] = TargetFiles(
        sprite_name=target_path.name,
        costumes=params.costumes,
        sounds=params.sounds
    )


@server.feature("itchy/targetFiles")
def receive_target_files(params: TargetFilesCacheParams):
    file_cache.clear()

    for target in params.targets:
        update_file_cache(target)


@server.feature("itchy/targetFilesChanged")
def receive_target_files_changed(params: TargetFilesParams):
    update_file_cache(params)


@server.feature(types.WORKSPACE_DID_RENAME_FILES)
def renamed_files(params: types.RenameFilesParams):
    for file in params.files:
        old_uri = file.old_uri
        fs_compatible_path = uri_to_fs(old_uri)

        path = Path(fs_compatible_path)
        if path.suffix != ".itch":
            continue

        new_uri = file.new_uri

        for variable in list(session.variables.values()):
            if compare_uris(variable.uri, old_uri):
                session.variables[variable.name] = replace(
                    variable,
                    uri=new_uri
                )

        for message in list(session.messages.values()):
            if compare_uris(message.uri, old_uri):
                session.messages[message.name] = replace(
                    message,
                    uri=new_uri
                )

        if new_uri != old_uri:
            assembler_snapshots[uri_to_fs(new_uri)] = assembler_snapshots.pop(fs_compatible_path, AssemblerState(new_uri, [], {}, {}, {}))


ignored_operations: set[tuple[str, str]] = set()


def should_ignore_rename(old_uri: str, new_uri: str) -> bool:
    rename = (old_uri, new_uri)

    if rename not in ignored_operations:
        return False

    ignored_operations.remove(rename)
    return True


@server.feature(types.WORKSPACE_WILL_RENAME_FILES,
                types.FileOperationRegistrationOptions(
                    filters=[
                        types.FileOperationFilter(
                            scheme="file",
                            pattern=types.FileOperationPattern(
                                glob="**/*.itch"
                            )
                        ),
                        types.FileOperationFilter(
                            scheme="file",
                            pattern=types.FileOperationPattern(
                                glob="**/*",
                                matches=types.FileOperationPatternKind.Folder
                            )
                        )
                    ]
                ))
def will_rename_files(params: types.RenameFilesParams) -> types.WorkspaceEdit | None:
    document_changes: list[types.RenameFile] = []

    for file in params.files:
        old_uri = file.old_uri
        old_fs_compatible_path = uri_to_fs(old_uri, True)
        new_fs_compatible_path = uri_to_fs(file.new_uri, True)

        current_file = Path(old_fs_compatible_path)
        new_file = Path(new_fs_compatible_path)

        if current_file.stem == new_file.stem:
            continue
        if should_ignore_rename(old_fs_compatible_path, new_fs_compatible_path):
            continue
        ignored_operations.add((old_fs_compatible_path, new_fs_compatible_path))

        if current_file.suffix == ".itch":
            # renaming file
            existing_folder = current_file.parent
            if not existing_folder.exists():
                continue
            new_location = existing_folder.parent / new_file.stem

            ignored_operations.add((str(existing_folder), str(new_location)))

            document_changes.append(types.RenameFile(
                old_uri=existing_folder.as_uri(),
                new_uri=new_location.as_uri()
            ))
        elif current_file.is_dir():
            # renaming folder
            existing_file = current_file / (current_file.stem + ".itch")
            if not existing_file.exists():
                continue

            new_location = current_file / (new_file.stem + ".itch")

            ignored_operations.add((str(existing_file), str(new_location)))

            document_changes.append(types.RenameFile(
                old_uri=existing_file.as_uri(),
                new_uri=new_location.as_uri()
            ))

    return types.WorkspaceEdit(
        document_changes = document_changes
    )


if __name__ == "__main__":
    logging.basicConfig(format="[%(levelname)s]: %(message)s", level=logging.DEBUG, filename=LOG_FILE)
    server.start_io()
