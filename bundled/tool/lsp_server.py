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
from dataclasses import dataclass, field
from typing import Iterable, Sequence
# from pygls.workspace.text_document import TextDocument, RE_START_WORD, RE_END_WORD
from pygls.lsp.server import LanguageServer
from lsprotocol import types
from itchy.shared_templates import DATA_TO_VARIABLE_TYPE, SourceSpan
from itchy.scratch_blocks import SCRATCH_BLOCKS, Event, Reporter, Field, ReturnType, Menu
from itchy.itch_ast import build_ast_with_semantic_tokens, utf16_length, ASTBuilder, SemanticToken
from itchy.parser import Parser, ExpectedToken, ParseError, ParseResult, ParsedNode
from itchy.tokenizer import Definitions
from itchy.assembler import Assembler, VariableTypes, ProcedureInfo, VariableData, MessageData, CompilerErrorCodes, SymbolOccurence, SymbolType
from itchy.dummy_nodes import make_dummy_primary, ANALYSIS_STRATEGIES, find_last_node, find_token, make_wrap
from itchy.errors import get_message, CompilerError, CompilerWarning


completion_ast = ASTBuilder()
func_signature_ast = ASTBuilder()
# parser that tries not to fail so ast can give syntax highlighting to entire file
semantic_parser = Parser(skip_bad_tokens=True, skip_rules_on_fail=ANALYSIS_STRATEGIES)
completions_parser = Parser(skip_bad_tokens=False, skip_rules_on_fail={"primary": make_dummy_primary})
func_signature_parser = Parser(skip_bad_tokens=False, skip_rules_on_fail={"primary": make_dummy_primary})

analysis_parser = Parser(skip_bad_tokens=True, skip_rules_on_fail=ANALYSIS_STRATEGIES)
analysis_ast = ASTBuilder()
# analysis_assembler = Assembler(is_strict=False)

server = LanguageServer("example-server", "v0.1")
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
    symbols: list[SymbolOccurence]
    variables: dict[tuple[str, str | None], VariableData]
    procedures: dict[str, ProcedureInfo]
    messages: dict[str, MessageData]

@dataclass()
class Session():
    variables: dict[str, VariableData] = field(default_factory=dict[str, VariableData]) 
    messages: dict[str, MessageData] = field(default_factory=dict[str, MessageData])

session = Session()
assembler_snapshots: dict[str, AssemblerState] = {

}


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
                if expected_type not in block.return_type:
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
        completions_parser.cancel()
        parsed = completions_parser.read(source)
        completion_ast.build(parsed.tree)
    except (ParseError, InterruptedError) as e:
        if isinstance(e, InterruptedError):
            return []
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


    for symbol in assembler.symbols:
        tree[1][symbol.span] = SemanticToken(
            symbol.span.start.line - 1,
            symbol.span.start.character - 1,
            utf16_length(symbol.name),
            symbol.symbol_type
        )

    
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

def get_editing_parameter(parser: Parser, ast_builder: ASTBuilder):
    function_name = None
    active_parameter = None
    parsed = parser.deepest_partial

    if parsed is not None:
        assert isinstance(parsed.tree, ParsedNode)
        try:
            if (node := find_last_node(parsed.tree, "functioncall")) is not None:
                tree = ast_builder.build_functioncall(node)
                function_name = tree.callee
                active_parameter = len(tree.args)

                if found_token := find_token(node, Definitions.CloseBracket):
                    if not found_token.dummy_token:
                        return None
                
            elif (node := find_last_node(parsed.tree, "eventstat")) is not None:

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
                if found_token := find_token(node, Definitions.CloseBracket):
                    if not found_token.dummy_token:
                        return None

        except ValueError:
            pass


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


    assembler_state = assembler_snapshots.get(uri)
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
            signature = f"({proc_type}) {proc_data.name}({", ".join(f"{proc_data.argument_names[i]}: {proc_data.argument_types[i].value}" 
                                                                for i in 
                                                                range(len(proc_data.argument_names)))}) -> {return_type}"
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

    for diagnostic in params.context.diagnostics:
        match diagnostic.code:
            case CompilerErrorCodes.UNDEFINED_VARIABLE:
                if diagnostic.data is None:
                    continue

                assembler_state = assembler_snapshots.get(uri)
                line = 0
                if assembler_state is not None:
                    line = sys.maxsize
                    
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
                        is_preferred=True
                    )
                )
            case _:
                pass

    return actions


def lint_document(uri: str):
    """
    Lints a single document. Returns True if the namespace has updated. 
    """
    log(f"Linting document: {uri}")
    document = server.workspace.get_text_document(uri)
    assembler = Assembler(uri, is_strict=False, compile_with_warnings=True)
    updated_globals = False
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

    # existing_snapshot = assembler_snapshots.get(uri)

    # if existing_snapshot is not None:
    #     for variable, variable_data in existing_snapshot.variables.items():
    #         if not variable_data.shared:
    #             continue
    #         if variable not in assembler.variable_map:
    #             updated_globals = True
    #             assembler.mark_variable_for_deletion.add(variable[0])
    
    for key, var_id in assembler.variable_map.items():
        variables[key] = assembler.variables[var_id]

    
    assembler_snapshots[uri] = AssemblerState(
        symbols=assembler.symbols,
        variables=variables,
        procedures=assembler.procedures,
        messages=assembler.messages
    )


    # temp_deleted_variables: set[str] = set()


    for var_name in assembler.mark_variable_for_deletion:
        if var_name in session.variables:
            # temp_deleted_variables.add(var_name)
            updated_globals = True
            del session.variables[var_name]


    for message in assembler.mark_message_for_deletion:
        if message in session.messages:
            del session.messages[message]


    for _, var_data in assembler.variables.items():
        if not var_data.shared:
            continue

        if var_data.name in assembler.mark_variable_for_deletion:
            continue

        session.variables[var_data.name] = var_data


    log(str([i for i in session.variables]))


    for message, message_id in assembler.messages.items():
        session.messages[message] = message_id


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
        message = get_message(error, analysis_parser.expected)
        diagnostics.append(
            types.Diagnostic(
                range=span_to_range(token.span),
                message=message,
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
                message=error.message,
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


def lint_documents_with_changes(uri: str):
    """
    Lints a single document, but will update other documents if the globals have changed.
    """
    globals_changed = lint_document(uri)
    if globals_changed:
        for other_uri in server.workspace.text_documents:
            if other_uri == uri:
                continue
            lint_document(other_uri)


@server.feature(types.WORKSPACE_DID_DELETE_FILES)
def did_delete_files(params: types.DeleteFilesParams):
    session.messages.clear()
    session.variables.clear()

    for file in params.files:
        uri = file.uri
        if uri in assembler_snapshots:
            del assembler_snapshots[uri]


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
    assembler_state = assembler_snapshots.get(params.text_document.uri)
    if assembler_state is None:
        return

    symbol = symbol_at_position(assembler_state.symbols, params.position)
    if symbol is None:
        return

    location = symbol.definition_location

    if location is None:
        for other_uri, state in assembler_snapshots.items():
            key = (symbol.name, None)
            
            variable = state.variables.get(key)
            if variable is None:
                continue

            if variable.definition_location is not None:
                uri = other_uri
                location = variable.definition_location
                break

    if location is None:
        return

    return types.Location(
        uri=uri,
        range=span_to_range(location)
    )


def replace_symbol(uri: str, symbol: SymbolOccurence, original: str, replace_with: str) -> list[types.TextEdit]:
    assembler_state = assembler_snapshots.get(uri)
    if assembler_state is None:
        return []

    edits: list[types.TextEdit] = []
    
    for other_symbol in assembler_state.symbols:
        if other_symbol.span.start.line == -1:
            continue

        if other_symbol.name != original:
            continue
        
        if symbol.symbol_type == other_symbol.symbol_type:
            edits.append(types.TextEdit(
                range=span_to_range(other_symbol.span),
                new_text=replace_with
            ))

    return edits


@server.feature(types.TEXT_DOCUMENT_RENAME)
def rename(params: types.RenameParams) -> types.WorkspaceEdit | types.ResponseError | None:
    current_uri = params.text_document.uri
    assembler_state = assembler_snapshots.get(current_uri)
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


    for uri in assembler_snapshots:
        if uri != current_uri and ignore_other_uris:
            continue
        edits[uri] = replace_symbol(uri, symbol, symbol.name, params.new_name)
        # lint_document(uri)
        lint_documents_with_changes(current_uri)

    return types.WorkspaceEdit(
        changes=edits
    )


if __name__ == "__main__":
    logging.basicConfig(format="[%(levelname)s]: %(message)s", level=logging.DEBUG, filename=LOG_FILE)
    server.start_io()
