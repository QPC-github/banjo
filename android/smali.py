import dataclasses
import pickle
import re
from pathlib import Path
from struct import unpack
from typing import (
    NewType,
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Match,
    Tuple,
    Union,
    cast,
)

try:
    from compat import (  # type: ignore
        log_debug,
        log_error,
        log_warn,
        InstructionTextToken,
        InstructionTextTokenType,
    )
except ModuleNotFoundError:
    from .compat import (
        log_debug,
        log_error,
        log_warn,
        InstructionTextToken,
        InstructionTextTokenType,
    )

# Only needed for type checking. Causes circular import
if TYPE_CHECKING:
    from .dex import DexFile, FileOffset

PICKLE_FILENAME = "instruction_data.pickle"
INSTRUCTIONS_PICKLE_PATH = Path(__file__).resolve().parent / PICKLE_FILENAME


@dataclasses.dataclass
class SmaliInstructionFormat:
    """Row of https://source.android.com/devices/tech/dalvik/instruction-formats#formats

    Example:
        _formatid: "12x"
        format_: "B|A|op"
        syntax: "op vA, vB"

        insn_len: 1
        num_regs: 2
        typecode: "x"
    """

    _formatid: str
    format_: str
    syntax: str
    # Parsed from id:
    insn_len: int
    num_regs: int
    typecode: str


@dataclasses.dataclass
class SmaliInstructionInfo:
    """Row of https://source.android.com/devices/tech/dalvik/dalvik-bytecode#instructions

    Example:
        _opcode: 1
        _formatid: "12x"
        fmt: (object)
        mnemonic: "move"
        syntax: "vA, vB"
        arguments: "A: destination register (4 bits)\nB: source register (4 bits)"
        description: "Move the contents of one non-object register to another."
    """

    _opcode: int
    _formatid: str
    fmt: SmaliInstructionFormat
    mnemonic: str
    syntax: str
    arguments: str
    description: str


@dataclasses.dataclass
class SmaliPackedSwitchPayload:
    _total_size: int
    size: int  # ushort
    first_key: int
    targets: List[int]


@dataclasses.dataclass
class SmaliSparseSwitchPayload:
    _total_size: int
    size: int  # ushort
    keys: List[int]
    targets: List[int]


@dataclasses.dataclass
class SmaliFillArrayDataPayload:
    _total_size: int
    element_width: int  # ushort
    size: int  # uint
    data: bytes  # ubyte


PseudoInstructions = NewType(
    "PseudoInstructions",
    Dict[
        "FileOffset",
        Union[
            SmaliPackedSwitchPayload,
            SmaliFillArrayDataPayload,
            SmaliSparseSwitchPayload,
        ],
    ],
)


def slice_nibbles(data: bytes, start_nibble: int, size: int = 1) -> int:
    """Slice out integer value of bytes indexed by nibble instead of byte.

    This function is only designed to work with current instruction formats. It
    makes a number of assumptions about byte order and positioning for these
    specific cases.
    """
    if size == 1:
        # Single nibble
        return int((data[start_nibble // 2] >> (((start_nibble + 1) % 2) * 4)) & 0xF)
    elif size == 2:
        # Single byte, assuming byte-alignment
        return data[start_nibble // 2]
    elif size == 4:
        # Normal 2-byte value, assuming byte-alignment
        return (data[start_nibble // 2] << 8) + data[start_nibble // 2 + 1]
    elif size == 8 or size == 16:
        # The 2-byte values are ordered from low to high
        res = 0
        for i, nibble in enumerate(range(start_nibble, start_nibble + size, 4)):
            res += ((data[nibble // 2] << 8) + data[nibble // 2 + 1]) << (i * 16)
        return res
    else:
        log_error(f"slice_nibbles called with unexpected size: {size}. Returning 0")
        return 0


def sign(val: int, size: int) -> int:
    """Convert unsigned val of size nibbles into a signed int."""
    mask = 1 << (4 * size - 1)
    return -(val & mask) + (val & ~mask)


def parse_with_format(data: bytes, fmt: str) -> Dict[str, int]:
    """Extract values from nibbles using format string.

    See TestFormatParsing for examples
    """
    values = dict()
    nibble = 0
    continuation = ""
    for byte in fmt.split(" "):
        for chunk in byte.split("|"):
            if "lo" in chunk or continuation:
                continuation += chunk
                if "hi" in continuation:
                    chunk = continuation.replace("lo", "").replace("hi", "")
                    continuation = ""
                else:
                    continue

            if chunk == "op":
                nibble += 2
            elif chunk == "ØØ":
                nibble += 2
            elif chunk.isupper():
                # Actually parse binary
                values[chunk[0]] = slice_nibbles(data, nibble, len(chunk))
                nibble += len(chunk)
            else:
                raise ValueError(f'failed reading format "{chunk}"')

    return values


def endian_swap_shorts(data: bytes) -> bytes:
    assert (len(data) % 2) == 0
    return bytes([data[i + (((i + 1) % 2) * 2 - 1)] for i in range(len(data))])


def format_args_with_syntax(args: Dict[str, int], syntax: str) -> str:
    """Format syntax strings with parsed arguments.

    "Syntax" and "Arguments" come from the table on
    https://source.android.com/devices/tech/dalvik/dalvik-bytecode#instructions
    This function takes in a dictionary mapping the character symbol for
    an argument to its integer value, and replaces all groups of
    capital letters in the syntax string with the corresponding values.

    Note that consecutive instances of the same replacement character
    are grouped. Replacing A with 5 in "AAAA A" results in "5 5".

    Substitutions preceded by a 'v' or '@' for registers or indices are
    treated as unsigned and all others cases are treated as signed. This
    isn't explicit in the documentation, but it seems to work.

    For easier formatting in `tokenize_syntax`, the integers are
    inserted in bare hexadecimal format. Further formatting is the
    responsibility of the calling function.

    See test case examples in TestFormattingArgsWithSyntax.
    """

    def fmt(m: Match[str]) -> str:
        val = args[m[0][-1]]
        # NOTE I think this is right, but it's not very clear in the docs
        if m[0][0] not in "v@":
            # Signed
            val = sign(val, len(m[0]) - 1)
        return f"{m[0][0]}{val:x}"

    return re.sub(".[A-Z]+", fmt, syntax)


def tokenize_syntax(
        df: "DexFile", word: str, args: Dict[str, int]
) -> List[InstructionTextToken]:
    tokens = list()
    tokens.append(InstructionTextToken(InstructionTextTokenType.TextToken, " "))

    # Check for prefixes and suffixes
    trailing_comma = False
    trailing_curly_brace = False
    if word[-1] == ",":
        trailing_comma = True
        word = word[:-1]
    if word[-1] == "}":  # Needs to be after ',' check
        trailing_curly_brace = True
        word = word[:-1]
    if word[0] == "{":
        tokens.append(InstructionTextToken(InstructionTextTokenType.TextToken, "{"))
        word = word[1:]

    # Format operand with numbers where the placeholders are
    word_formatted = format_args_with_syntax(args, word)

    # Add operand token
    if word_formatted == "":
        # {}
        pass
    elif word_formatted[0] == "v":
        # Register e.g. v01
        val = int(word_formatted[1:], 16)
        if val >= 256:
            # TODO add link to issue. See comment in Smali
            log_warn(
                f"Rendering v{val}, but Binary Ninja only knows about registers up to 255 for analysis."
            )
        tokens.append(
            InstructionTextToken(InstructionTextTokenType.RegisterToken, f"v{val}")
        )
    elif word_formatted[:2] == "#+":
        # Literal e.g. #+0001
        tokens.append(
            InstructionTextToken(
                InstructionTextTokenType.IntegerToken, hex(int(word_formatted[2:], 16))
            )
        )
    elif "@" in word_formatted:
        # Lookup value e.g. call_site@0001
        # Possible lookup types: call_site, field, method, method_handle, proto, string, type
        lookup_type, lookup_index_str = word_formatted.split("@")
        lookup_index = int(lookup_index_str, 16)
        if lookup_type == "call_site":
            log_warn(lookup_type + " isn't implemented yet")
            tokens.append(
                InstructionTextToken(InstructionTextTokenType.TextToken, word_formatted)
            )
        elif lookup_type == "field":
            field = df.field_ids[lookup_index]
            # Class name
            tokens.append(
                InstructionTextToken(InstructionTextTokenType.TextToken, field.class_)
            )
            tokens.append(
                InstructionTextToken(InstructionTextTokenType.TextToken, "->")
            )
            # Field name
            tokens.append(
                InstructionTextToken(InstructionTextTokenType.TextToken, field.name)
            )
            tokens.append(InstructionTextToken(InstructionTextTokenType.TextToken, ":"))
            # Type
            tokens.append(
                InstructionTextToken(InstructionTextTokenType.TextToken, field.type_)
            )
        elif lookup_type == "meth":
            meth = df.method_ids[lookup_index]
            # Class and method names
            tokens.append(
                InstructionTextToken(InstructionTextTokenType.TextToken, meth.class_)
            )
            tokens.append(
                InstructionTextToken(InstructionTextTokenType.TextToken, "->")
            )

            if meth._insns_off is not None:
                tokens.append(
                    InstructionTextToken(
                        InstructionTextTokenType.PossibleAddressToken,
                        meth.name,
                        value=meth._insns_off,
                    )
                )
            else:
                tokens.append(
                    InstructionTextToken(InstructionTextTokenType.TextToken, meth.name)
                )
            # Parameters
            tokens.append(InstructionTextToken(InstructionTextTokenType.TextToken, "("))
            for param in meth.proto.parameters:
                tokens.append(
                    InstructionTextToken(InstructionTextTokenType.TextToken, param)
                )
            # if meth.proto.parameters:
            #     # Remove trailing semicolon
            #     tokens.pop()
            tokens.append(InstructionTextToken(InstructionTextTokenType.TextToken, ")"))
            # Return type
            tokens.append(
                InstructionTextToken(
                    InstructionTextTokenType.TextToken, meth.proto.return_type
                )
            )
        elif lookup_type == "method_handle":
            log_warn(lookup_type + " isn't implemented yet")
            tokens.append(
                InstructionTextToken(InstructionTextTokenType.TextToken, word_formatted)
            )
        elif lookup_type == "proto":
            log_warn(lookup_type + " isn't implemented yet")
            tokens.append(
                InstructionTextToken(InstructionTextTokenType.TextToken, word_formatted)
            )
        elif lookup_type == "string":
            string_ = df.strings[lookup_index]
            tokens.append(InstructionTextToken(InstructionTextTokenType.TextToken, '"'))
            tokens.append(
                # Escape e.g \n -> \\n or binja will render literal newline
                InstructionTextToken(
                    InstructionTextTokenType.TextToken,
                    string_.encode("unicode-escape").decode(),
                )
            )
            tokens.append(InstructionTextToken(InstructionTextTokenType.TextToken, '"'))
        elif lookup_type == "type":
            type_ = df.type_ids[lookup_index]
            tokens.append(
                InstructionTextToken(InstructionTextTokenType.TextToken, type_)
            )
        else:
            log_error(f"Unknown lookup type: {word_formatted}")
            tokens.append(
                InstructionTextToken(InstructionTextTokenType.TextToken, word_formatted)
            )
    elif word_formatted[0] == "+":
        # Address offset e.g. +0011
        if int(word_formatted[1:], 16) >= 0:
            tokens.append(InstructionTextToken(InstructionTextTokenType.TextToken, "+"))
        tokens.append(
            InstructionTextToken(
                InstructionTextTokenType.PossibleAddressToken, word_formatted[1:]
            )
        )
    elif word_formatted == "..":
        tokens.append(InstructionTextToken(InstructionTextTokenType.TextToken, ".."))
    else:
        # Other tokens. Investigate these
        log_warn(f'Formatting unknown token with syntax: "{word}": {word_formatted}')
        tokens.append(
            InstructionTextToken(InstructionTextTokenType.TextToken, word_formatted)
        )

    # Add suffixes
    if trailing_curly_brace:
        tokens.append(InstructionTextToken(InstructionTextTokenType.TextToken, "}"))
    if trailing_comma:
        tokens.append(
            InstructionTextToken(InstructionTextTokenType.OperandSeparatorToken, ",")
        )
    return tokens


def disassemble(
        df: "DexFile", data: bytes, addr: "FileOffset"
) -> Tuple[List[InstructionTextToken], int]:
    # Static variable
    if "insns" not in disassemble.__dict__:
        # https://github.com/python/mypy/issues/708
        disassemble.insns = load_insns()  # type: ignore[attr-defined]

    if len(data) < 2:
        log_warn(
            f"Trying to disassemble data of length {len(data)} at {addr}: {data!r}"
        )
        # Fun fact: if you return -1 here, binja segfaults
        return [], 0

    # Handle pseudo-instructions first
    if data[0] == 0 and data[1] != 0:
        if data[1] == 1:
            # packed-switch
            ps = cast(SmaliPackedSwitchPayload, df.pseudoinstructions[addr])
            text = f".packed-switch {hex(ps.first_key)}\n"
            text += "".join(
                [f"        :pswitch_offset_{target:x}\n" for target in ps.targets]
            )
            text += "    .end packed-switch"
        elif data[1] == 2:
            # sparse-switch
            ss = cast(SmaliSparseSwitchPayload, df.pseudoinstructions[addr])
            text = ".sparse-switch\n"
            text += "".join(
                [
                    f"        {hex(ss.keys[i])} -> :sswitch_offset_{ss.targets[i]:x}\n"
                    for i in range(ss.size)
                ]
            )
            text += "    .end sparse-switch"
        elif data[1] == 3:
            fa = cast(SmaliFillArrayDataPayload, df.pseudoinstructions[addr])
            text = f"pseudo-instruction: {fa}"
        else:
            raise ValueError(f"Invalid pseudo-instruction with type {data[1]}")
        return (
            [
                InstructionTextToken(
                    token_type=InstructionTextTokenType.InstructionToken, text=text,
                ),
            ],
            df.pseudoinstructions[addr]._total_size,
        )

    # Now handle normal instructions
    tokens = list()
    insn_info = disassemble.insns[data[0]]  # type: ignore[attr-defined]
    tokens.append(
        InstructionTextToken(
            InstructionTextTokenType.InstructionToken, insn_info.mnemonic
        )
    )

    data_to_parse = endian_swap_shorts(data[: 2 * insn_info.fmt.insn_len])
    if len(data_to_parse) != insn_info.fmt.insn_len * 2:
        log_error(
            "Disassembly failed. Too few bytes part of instruction available to parse"
        )
        return list(), insn_info.fmt.insn_len * 2
    args = parse_with_format(data_to_parse, insn_info.fmt.format_)
    if "r" in insn_info._formatid:
        # Range instructions
        args["N"] = args["A"] + args["C"] - 1

    # Fix up syntax
    if insn_info._formatid == "35c":
        # 35c is weird for a couple reasons
        # 1. It uses "kind" instead of the actual kind of the name of the
        #    constant pool
        # 2. It forgets about "kind" for A=5 and lists them all out
        m = re.search("\\s([a-z]+)@", insn_info.syntax)
        if m is None:
            log_error(f"Failed to parse 35c at {addr}")
        else:
            kind = m.group(1)
        if args["A"] == 5:
            syntax = f"{{vC, vD, vE, vF, vG}}, {kind}@BBBB"
        elif args["A"] == 4:
            syntax = f"{{vC, vD, vE, vF}}, {kind}@BBBB"
        elif args["A"] == 3:
            syntax = f"{{vC, vD, vE}}, {kind}@BBBB"
        elif args["A"] == 2:
            syntax = f"{{vC, vD}}, {kind}@BBBB"
        elif args["A"] == 1:
            syntax = f"{{vC}}, {kind}@BBBB"
        elif args["A"] == 0:
            syntax = f"{{}}, {kind}@BBBB"
        else:
            log_error(f"Failed to parse syntax for 35c instruction at {addr}")
            syntax = "error (35c)"
    elif "[A=" in insn_info.fmt.syntax:
        for line in insn_info.fmt.syntax.split("[A="):
            line = line.strip()
            if line and line[0] == str(args["A"]):
                syntax = line[6:]
                break
        else:
            log_error(f"Failed to parse syntax for instruction at {addr}")
            syntax = "error"
    else:
        syntax = insn_info.syntax

    for word in syntax.split(" "):
        if not word or word.isspace():
            continue
        tokens += tokenize_syntax(df, word, args)

    return tokens, insn_info.fmt.insn_len * 2


def disassemble_pseudoinstructions(
        data: bytes, addr: "FileOffset"
) -> PseudoInstructions:
    # Static variable
    if "insns" not in disassemble.__dict__:
        disassemble.insns = load_insns()  # type: ignore[attr-defined]

    pseudoinstructions: PseudoInstructions = cast(PseudoInstructions, dict())
    code_offset = 0
    while code_offset < len(data):
        if data[code_offset + 1] == 0 and data[code_offset] != 0:
            # Pseudo-instruction
            # TODO performance benchmark swapping here vs. doing it once at
            # beginning of function
            data_swapped = endian_swap_shorts(data[code_offset + 2:])
            if data[code_offset] == 1:
                # packed-switch-payload
                size = unpack("<H", data_swapped[:2])[0]
                pseudoinstructions[
                    cast("FileOffset", addr + code_offset)
                ] = SmaliPackedSwitchPayload(
                    _total_size=size * 4 + 8,
                    size=size,
                    first_key=unpack("<i", data_swapped[2:6])[0],
                    targets=[
                        unpack("<i", data_swapped[i: i + 4])[0]
                        for i in range(6, 6 + size * 4, 4)
                    ],
                )
                code_offset += size * 4 + 8
            elif data[code_offset] == 2:
                # sparse-switch-payload
                size = unpack("<H", data_swapped[:2])[0]
                pseudoinstructions[
                    cast("FileOffset", addr + code_offset)
                ] = SmaliSparseSwitchPayload(
                    _total_size=size * 8 + 4,
                    size=size,
                    keys=[
                        unpack("<i", data_swapped[i: i + 4])[0]
                        for i in range(2, 2 + size * 4, 4)
                    ],
                    targets=[
                        unpack("<i", data_swapped[i: i + 4])[0]
                        for i in range(2 + size * 4, 2 + size * 8, 4)
                    ],
                )
                code_offset += size * 8 + 4
            elif data[code_offset] == 3:
                # fill-array-data-payload
                element_width = unpack("<H", data_swapped[:2])[0]
                size = unpack("<I", data_swapped[2:6])[0]
                pseudoinstructions[
                    cast("FileOffset", addr + code_offset)
                ] = SmaliFillArrayDataPayload(
                    _total_size=((size * element_width + 1) // 2) * 2 + 8,
                    element_width=element_width,
                    size=size,
                    data=data_swapped[6: 8 + ((element_width * size + 1) // 2) * 2],
                )
                code_offset += ((size * element_width + 1) // 2) * 2 + 8
            else:
                log_error(
                    f"Unknown pseudoinstruction {data[code_offset:code_offset + 2]!r} at {addr + code_offset} in code block at {addr}"
                )
                code_offset += 2
        else:
            # Normal instruction
            insn_info = disassemble.insns[data[code_offset + 1]]  # type: ignore[attr-defined]
            code_offset += insn_info.fmt.insn_len * 2
    return pseudoinstructions


class SmaliUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        if name == "SmaliInstructionFormat":
            return SmaliInstructionFormat
        elif name == "SmaliInstructionInfo":
            return SmaliInstructionInfo
        return super().find_class(module, name)


def load_insns() -> Dict[int, SmaliInstructionInfo]:
    if not INSTRUCTIONS_PICKLE_PATH.is_file():
        log_warn(
            "Instructions cache does not exist. Generating now (requires internet access)"
        )
        from .generate_instruction_info import gen_instruction_info

        gen_instruction_info()
    with INSTRUCTIONS_PICKLE_PATH.open("br") as f:
        return cast(Dict[int, SmaliInstructionInfo], SmaliUnpickler(f).load())
