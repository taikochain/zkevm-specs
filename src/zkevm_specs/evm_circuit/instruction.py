from __future__ import annotations
from enum import IntEnum, auto
from typing import Optional, Sequence, Tuple, Union, List, cast

from eth_utils import (
    keccak,
)
import rlp  # type: ignore

from ..util import (
    FQ,
    IntOrFQ,
    add_words,
    sum_values,
    Word,
    WordOrValue,
    Expression,
    ExpressionImpl,
    cast_expr,
    MAX_N_BYTES,
    N_BYTES_ACCOUNT_ADDRESS,
    N_BYTES_MEMORY_ADDRESS,
    N_BYTES_MEMORY_SIZE,
    N_BYTES_GAS,
    GAS_COST_COPY,
    MEMORY_EXPANSION_QUAD_DENOMINATOR,
    MEMORY_EXPANSION_LINEAR_COEFF,
)
from .execution_state import ExecutionState
from .opcode import Opcode
from .precompile import Precompile
from .step import StepState
from .table import (
    AccountFieldTag,
    BlockContextFieldTag,
    BytecodeFieldTag,
    CallContextFieldTag,
    FixedTableRow,
    RWTableRow,
    Tables,
    FixedTableTag,
    TxContextFieldTag,
    RW,
    RWTableTag,
    TxLogFieldTag,
    TxReceiptFieldTag,
    CopyDataTypeTag,
)


class ConstraintUnsatFailure(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class TransitionKind(IntEnum):
    Same = auto()
    SameWord = auto()
    Delta = auto()
    To = auto()
    ToWord = auto()


class Transition:
    kind: TransitionKind
    value: Union[int, Expression, Word]

    def __init__(self, kind: TransitionKind, value: Union[int, Expression, Word] = 0) -> None:
        self.kind = kind
        self.value = value

    @staticmethod
    def same() -> Transition:
        return Transition(TransitionKind.Same)

    @staticmethod
    def same_word() -> Transition:
        return Transition(TransitionKind.SameWord)

    @staticmethod
    def delta(delta: Union[int, Expression]):
        return Transition(TransitionKind.Delta, delta)

    @staticmethod
    def to(to: Union[int, Expression]):
        return Transition(TransitionKind.To, to)

    @staticmethod
    def to_word(to: Word):
        return Transition(TransitionKind.ToWord, to)


class ReversionInfo:
    rw_counter_end_of_reversion: FQ
    is_persistent: FQ
    reversible_write_counter: FQ

    def __init__(
        self,
        rw_counter_end_of_reversion: Expression,
        is_persistent: Expression,
        reversible_write_counter: Expression,
    ) -> None:
        self.rw_counter_end_of_reversion = rw_counter_end_of_reversion.expr()
        self.is_persistent = is_persistent.expr()
        self.reversible_write_counter = reversible_write_counter.expr()

    def rw_counter_of_reversion(self) -> FQ:
        rw_counter_of_reversion = self.rw_counter_end_of_reversion - self.reversible_write_counter
        self.reversible_write_counter += 1
        return rw_counter_of_reversion


class Instruction:
    tables: Tables
    curr: StepState
    next: StepState

    # meta information
    is_first_step: bool
    is_last_step: bool

    # helper numbers
    rw_counter_offset: int = 0
    program_counter_offset: int = 0
    stack_pointer_offset: int = 0
    log_index_offset: int = 0

    def __init__(
        self,
        tables: Tables,
        curr: StepState,
        next: StepState,
        is_first_step: bool,
        is_last_step: bool,
    ) -> None:
        self.tables = tables
        self.curr = curr
        self.next = next
        self.is_first_step = is_first_step
        self.is_last_step = is_last_step

    def constrain_zero(self, value: Expression):
        assert value.expr() == 0, ConstraintUnsatFailure(f"Expected value to be 0, but got {value}")

    def constrain_not_zero(self, value: Expression):
        assert value.expr() != 0, ConstraintUnsatFailure(
            f"Expected value to be != 0, but got {value}"
        )

    def constrain_not_zero_word(self, value: Word):
        assert value.lo.expr() != 0 or value.hi.expr() != 0, ConstraintUnsatFailure(
            f"Expected word to be != 0, but got {value}"
        )

    def constrain_equal(self, lhs: Expression, rhs: Expression):
        assert lhs.expr() == rhs.expr(), ConstraintUnsatFailure(
            f"Expected values to be equal, but got {lhs} and {rhs}"
        )

    def constrain_equal_word(self, lhs: Word, rhs: Word):
        assert (
            lhs.lo.expr() == rhs.lo.expr() and lhs.hi.expr() == rhs.hi.expr()
        ), ConstraintUnsatFailure(f"Expected words to be equal, but got {lhs} and {rhs}")

    def constrain_in(self, lhs: Expression, rhs: List[FQ]):
        assert lhs.expr() in rhs, ConstraintUnsatFailure(
            f"Expected value to be in {rhs}, but got {lhs}"
        )

    def constrain_in_word(self, lhs: Word, rhs: List[Word]):
        assert lhs in rhs, ConstraintUnsatFailure(f"Expected word to be in {rhs}, but got {lhs}")

    def constrain_bool(self, num: Expression):
        assert num.expr() in [0, 1], ConstraintUnsatFailure(
            f"Expected value to be a bool, but got {num}"
        )

    def constrain_gas_left_not_underflow(self, gas_left: Expression):
        self.range_check(gas_left, N_BYTES_GAS)

    def constrain_execution_state_transition(self):
        curr, next = self.curr.execution_state, self.next.execution_state

        # ExecutionState transition constraint for special ones
        if curr == ExecutionState.EndTx:
            assert next in [ExecutionState.BeginTx, ExecutionState.EndBlock]
        elif curr == ExecutionState.EndBlock:
            assert next == ExecutionState.EndBlock

        # Negation ExecutionState transition constraint for rest ones
        if next == ExecutionState.BeginTx:
            assert curr == ExecutionState.EndTx
        elif next == ExecutionState.EndTx:
            assert curr.halts() or curr == ExecutionState.BeginTx
        elif next == ExecutionState.EndBlock:
            assert curr in [ExecutionState.EndTx, ExecutionState.EndBlock]

    def constrain_step_state_transition(self, **kwargs: Transition):
        keys = set(
            [
                "rw_counter",
                "call_id",
                "is_root",
                "is_create",
                "code_hash",
                "program_counter",
                "stack_pointer",
                "gas_left",
                "memory_word_size",
                "reversible_write_counter",
                "log_id",
            ]
        )

        assert keys.issuperset(
            kwargs.keys()
        ), f"Invalid keys {list(set(kwargs.keys()).difference(keys))} for step state transition"

        for key, transition in kwargs.items():
            curr, next = getattr(self.curr, key), getattr(self.next, key)
            if isinstance(curr, int):
                curr = FQ(curr)
            if isinstance(next, int):
                next = FQ(next)
            if isinstance(transition.value, int):
                transition.value = FQ(transition.value)
            if transition.kind == TransitionKind.Same:
                assert next.expr() == curr.expr(), ConstraintUnsatFailure(
                    f"State {key} should be same as {curr}, but got {next}"
                )
            elif transition.kind == TransitionKind.SameWord:
                curr, next = cast(Word, curr), cast(Word, next)
                assert next.lo.expr() == curr.lo.expr() and next.hi.expr() == curr.hi.expr(), ConstraintUnsatFailure(  # type: ignore
                    f"State {key} should be same as {curr}, but got {next}"
                )
            elif transition.kind == TransitionKind.Delta:
                curr, next = cast(FQ, curr), cast(FQ, next)
                value = cast(FQ, transition.value)
                assert next.expr() == curr.expr() + value.expr(), ConstraintUnsatFailure(
                    f"State {key} should transit to {curr} + {transition.value} ({curr + transition.value}), but got {next}"
                )
            elif transition.kind == TransitionKind.To:
                curr, next = cast(FQ, curr), cast(FQ, next)
                value = cast(FQ, transition.value)
                assert next.expr() == value.expr(), ConstraintUnsatFailure(
                    f"State {key} should transit to {transition.value}, but got {next}"
                )
            elif transition.kind == TransitionKind.ToWord:
                curr, next = cast(Word, curr), cast(Word, next)
                # mypy gets confused here and thinkgs value must be FQ.
                value = cast(Word, transition.value)  # type: ignore
                assert next.lo.expr() == value.lo.expr() and next.hi.expr() == value.hi.expr(), ConstraintUnsatFailure(  # type: ignore
                    f"State {key} should transit to {transition.value}, but got {next}"
                )
            else:
                raise ValueError("Unreacheable")

    def step_state_transition_to_new_context(
        self,
        rw_counter: Transition,
        call_id: Transition,
        is_root: Transition,
        is_create: Transition,
        code_hash: Transition,
        gas_left: Transition,
        reversible_write_counter: Transition,
        log_id: Transition,
    ):
        self.constrain_step_state_transition(
            rw_counter=rw_counter,
            call_id=call_id,
            is_root=is_root,
            is_create=is_create,
            code_hash=code_hash,
            gas_left=gas_left,
            reversible_write_counter=reversible_write_counter,
            log_id=log_id,
            # Initialization unconditionally
            program_counter=Transition.to(0),
            stack_pointer=Transition.to(1024),
            memory_word_size=Transition.to(0),
        )

    def step_state_transition_to_restored_context(
        self,
        rw_counter_delta: int,
        return_data_offset: Expression,
        return_data_length: Expression,
        gas_left: Expression,
        caller_id: Optional[Expression] = None,
    ):
        rw_counter_delta += 11 + int(caller_id is None)
        # Read caller's context for restore
        if caller_id is None:
            caller_id = self.call_context_lookup(CallContextFieldTag.CallerId)

        [
            caller_is_root,
            caller_is_create,
            caller_code_hash,
            caller_program_counter,
            caller_stack_pointer,
            caller_gas_left,
            caller_memory_size,
            caller_reversible_write_counter,
        ] = [
            self.call_context_lookup_word(field_tag, call_id=caller_id)
            for field_tag in [
                CallContextFieldTag.IsRoot,
                CallContextFieldTag.IsCreate,
                CallContextFieldTag.CodeHash,
                CallContextFieldTag.ProgramCounter,
                CallContextFieldTag.StackPointer,
                CallContextFieldTag.GasLeft,
                CallContextFieldTag.MemorySize,
                CallContextFieldTag.ReversibleWriteCounter,
            ]
        ]

        # Update caller's last callee information
        for field_tag, expected_value in [
            (CallContextFieldTag.LastCalleeId, self.curr.call_id),
            (CallContextFieldTag.LastCalleeReturnDataOffset, return_data_offset),
            (CallContextFieldTag.LastCalleeReturnDataLength, return_data_length),
        ]:
            self.constrain_equal(
                self.call_context_lookup(field_tag, RW.Write, call_id=caller_id),
                expected_value,
            )

        # Accumulate reversible_write_counter in case this call stack reverts
        # in the future even it itself succeeds.
        # Note that when sub-call halts in failure, we don't need to
        # accumulate reversible_write_counter because what happened in the
        # sub-call has been reverted.
        reversible_write_counter = FQ(0)
        if self.curr.execution_state.halts_in_success():
            reversible_write_counter = self.curr.reversible_write_counter

        self.constrain_step_state_transition(
            rw_counter=Transition.delta(rw_counter_delta),
            call_id=Transition.to(caller_id),
            is_root=Transition.to(caller_is_root.value()),
            is_create=Transition.to(caller_is_create.value()),
            code_hash=Transition.to_word(caller_code_hash),
            program_counter=Transition.to(caller_program_counter.value()),
            stack_pointer=Transition.to(caller_stack_pointer.value()),
            # Pays back gas_left to caller
            gas_left=Transition.to(caller_gas_left.value() + gas_left.expr()),
            memory_word_size=Transition.to(caller_memory_size.value()),
            # Accumulate reversible_write_counter to caller
            reversible_write_counter=Transition.to(
                caller_reversible_write_counter.value() + reversible_write_counter.expr()
            ),
        )

    def step_state_transition_in_same_context(
        self,
        opcode: Expression,
        rw_counter: Transition = Transition.same(),
        program_counter: Transition = Transition.same(),
        stack_pointer: Transition = Transition.same(),
        memory_word_size: Transition = Transition.same(),
        reversible_write_counter: Transition = Transition.same(),
        dynamic_gas_cost: IntOrFQ = 0,
        log_id: Transition = Transition.same(),
    ):
        self.responsible_opcode_lookup(opcode)

        gas_cost = FQ(Opcode(opcode.expr().n).constant_gas_cost() + dynamic_gas_cost)
        self.constrain_gas_left_not_underflow(self.curr.gas_left - gas_cost)

        self.constrain_step_state_transition(
            rw_counter=rw_counter,
            program_counter=program_counter,
            stack_pointer=stack_pointer,
            gas_left=Transition.delta(-gas_cost),
            memory_word_size=memory_word_size,
            reversible_write_counter=reversible_write_counter,
            log_id=log_id,
            # Always stay same
            call_id=Transition.same(),
            is_root=Transition.same(),
            is_create=Transition.same(),
            code_hash=Transition.same_word(),
        )

    def sum(self, values: Sequence[IntOrFQ]) -> FQ:
        return sum_values(values)

    def is_zero(self, value: Expression) -> FQ:
        return FQ(value.expr() == 0)

    def is_equal(self, lhs: Expression, rhs: Expression) -> FQ:
        return self.is_zero(lhs.expr() - rhs.expr())

    def is_equal_word(self, lhs: Word, rhs: Word) -> FQ:
        return self.is_zero_word(
            Word((lhs.lo.expr() - rhs.lo.expr(), lhs.hi.expr() - rhs.hi.expr()), check=False)
        )

    def continuous_selectors(self, value: Expression, n: int) -> Sequence[FQ]:
        return [FQ(i < value.expr().n) for i in range(n)]

    def select(
        self, condition: FQ, when_true: ExpressionImpl, when_false: ExpressionImpl
    ) -> ExpressionImpl:
        assert condition in [0, 1], "Condition of select should be a checked bool"
        return when_true if condition == 1 else when_false

    def select_word(self, condition: FQ, when_true: Word, when_false: Word) -> Word:
        assert condition in [0, 1], "Condition of select_word should be a checked bool"
        return when_true if condition == 1 else when_false

    def pair_select(self, value: Expression, lhs: Expression, rhs: Expression) -> Tuple[FQ, FQ]:
        lhs_eq, rhs_eq = self.multiple_select(value, (lhs, rhs))
        return lhs_eq, rhs_eq

    def multiple_select(self, value: Expression, options: Tuple[Expression, ...]) -> Tuple[FQ, ...]:
        return tuple(FQ(value.expr() == o.expr()) for o in options)

    def constant_divmod(
        self, numerator: Expression, denominator: Expression, n_bytes: int
    ) -> Tuple[FQ, FQ]:
        quotient, remainder = divmod(numerator.expr().n, denominator.expr().n)
        self.range_check(FQ(quotient), n_bytes)
        return FQ(quotient), FQ(remainder)

    def compare(self, lhs: Expression, rhs: Expression, n_bytes: int) -> Tuple[FQ, FQ]:
        assert n_bytes <= MAX_N_BYTES, "Too many bytes to composite an integer in field"
        assert lhs.expr().n < 256**n_bytes, f"lhs {lhs} exceeds the range of {n_bytes} bytes"
        assert rhs.expr().n < 256**n_bytes, f"rhs {rhs} exceeds the range of {n_bytes} bytes"
        return FQ(lhs.expr().n < rhs.expr().n), FQ(lhs.expr().n == rhs.expr().n)

    def compare_word(self, lhs: Word, rhs: Word) -> Tuple[FQ, FQ]:
        """
        Compare the value of two 256-bit words, and return two outputs.
        The first output value is 1 if the left-hand side is strictly smaller, 0 otherwise.
        The second output value is 1 if the left-hand side is equal to the right-hand side, 0 otherwise.
        """
        lhs_lo, lhs_hi = lhs.to_lo_hi()
        rhs_lo, rhs_hi = rhs.to_lo_hi()
        hi_lt, hi_eq = self.compare(lhs_hi, rhs_hi, 16)
        lo_lt, lo_eq = self.compare(lhs_lo, rhs_lo, 16)
        return FQ(hi_lt + hi_eq * lo_lt), FQ(hi_eq * lo_eq)

    def precompile(self, address: Expression) -> FQ:
        try:
            Precompile(address.expr().n)
            return FQ(1)
        except ValueError:
            return FQ(0)

    def min_word(self, lhs: Word, rhs: Word) -> Word:
        lt, _ = self.compare_word(lhs, rhs)
        return self.select_word(lt, lhs, rhs)

    def min(self, lhs: Expression, rhs: Expression, n_bytes: int) -> FQ:
        lt, _ = self.compare(lhs, rhs, n_bytes)
        return cast_expr(self.select(lt, lhs, rhs), FQ)

    def max(self, lhs: Expression, rhs: Expression, n_bytes: int) -> FQ:
        lt, _ = self.compare(lhs, rhs, n_bytes)
        return cast_expr(self.select(lt, rhs, lhs), FQ)

    def word_to_fq(self, word: Word, n_bytes: int) -> FQ:
        word_le_bytes = word.to_le_bytes()
        if sum(word_le_bytes[n_bytes:]) != FQ(0):
            raise ConstraintUnsatFailure(f"Word {word} has too many bytes to fit {n_bytes} bytes")
        return self.bytes_to_fq(word_le_bytes[:n_bytes])

    def is_neg_word(self, word: Word) -> FQ:
        return self.compare(FQ(0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF), word.hi.expr(), 16)[0]

    def is_zero_word(self, word: Word) -> FQ:
        return self.is_zero(self.sum([word.lo.expr(), word.hi.expr()]))

    def byte_size(self, word: Word) -> FQ:
        le_bytes = [b.n for b in word.to_le_bytes()]
        return FQ(len(bytearray(le_bytes).rstrip(b"\x00")))

    def bytes_to_fq(self, value: Union[bytes, Sequence[FQ]], constrained=False) -> FQ:
        if not isinstance(value, bytes):
            value = bytes([b.n for b in value])
        assert len(value) <= MAX_N_BYTES, "Too many bytes to composite an integer in field"

        fq = FQ(int.from_bytes(value, "little"))

        if constrained:
            expr = sum(list(map(lambda x: (256 ** x[0]) * x[1], enumerate(list(value)))))
            self.constrain_equal(fq, FQ(expr))

        return fq

    def address_to_word(self, addr: Expression) -> Word:
        """Verify that address is 160 bits and return it as a Word (lo, hi)"""
        addr_bytes = addr.expr().n.to_bytes(32, "little")
        self.constrain_zero(FQ(sum(addr_bytes[N_BYTES_ACCOUNT_ADDRESS:])))
        return Word(addr_bytes)

    def word_to_address(self, word: Word) -> Expression:
        """Verify that word is 160 bits and return it as a single value"""
        return self.word_to_fq(word, N_BYTES_ACCOUNT_ADDRESS)

    def word_to_u64(self, word: Word) -> Expression:
        """Verify that word is 64 bits and return it as a single value"""
        return self.word_to_fq(word, 8)

    def range_lookup(self, value: Expression, range: int):
        self.fixed_lookup(FixedTableTag.range_table_tag(range), value)

    def byte_range_lookup(self, value: Expression):
        self.range_lookup(value, 256)

    def range_check(self, value: Expression, n_bytes: int) -> bytes:
        assert n_bytes <= MAX_N_BYTES, "Too many bytes to composite an integer in field"
        try:
            return value.expr().n.to_bytes(n_bytes, "little")
        except OverflowError:
            raise ConstraintUnsatFailure(f"Value {value} has too many bytes to fit {n_bytes} bytes")

    # Return a tuple of `abs(x)` and `x_is_neg`. For a special case when
    # `x = -(1 << 255)`, this function returns the same value of `-(1 << 255)`,
    # since it is signed overflow.
    def abs_word(self, x: Word) -> Tuple[Word, FQ]:
        is_neg = self.is_neg_word(x)

        # Generate the witness `x_abs`.
        x_abs = x if is_neg == 0 else Word((1 << 256) - x.int_value())

        x_abs_lo, x_abs_hi = x_abs.to_lo_hi()
        x_lo, x_hi = x.to_lo_hi()

        # Constrain `x_abs_lo == x_lo` and `x_abs_hi == x_hi` if non negative.
        self.constrain_zero((x_abs_lo - x_lo) * (1 - is_neg))
        self.constrain_zero((x_abs_hi - x_hi) * (1 - is_neg))

        # When `is_neg`, contrain `x + x_abs == 1 << 256`. Even if
        # `x = -(1 << 255)` that is signed overflow, and
        # `abs(-(1 << 255) = -(1 << 255)`.
        carry_lo, sum_lo = divmod(x_lo.n + x_abs_lo.n, 1 << 128)
        carry_hi, sum_hi = divmod(x_hi.n + x_abs_hi.n + carry_lo, 1 << 128)

        # Contrain `sum([x_lo, x_abs_lo]) == sum_lo + carry_lo * 2^128`.
        self.constrain_zero(FQ(sum_lo) + FQ(carry_lo) * FQ(1 << 128) - self.sum([x_lo, x_abs_lo]))

        # Contrain `sum([x_hi, x_abs_hi]) + carry_lo == sum_hi + carry_hi * 2^128`.
        self.constrain_zero(
            FQ(sum_hi) + FQ(carry_hi) * FQ(1 << 128) - FQ(carry_lo) - self.sum([x_hi, x_abs_hi])
        )

        # When `is_neg`, constrain both low and high remainders are zero, and
        # `carry_hi == 1`. Since the final result is `1 << 256`.
        self.constrain_zero(FQ(sum_lo + sum_hi) * is_neg)
        self.constrain_zero(FQ(1 - carry_hi) * is_neg)

        return x_abs, is_neg

    def add_words(self, addends: Sequence[Word]) -> Tuple[Word, FQ]:
        return add_words(addends)

    def sub_word(self, minuend: Word, subtrahend: Word) -> Tuple[Word, FQ]:
        minuend_lo, minuend_hi = minuend.to_lo_hi()
        subtrahend_lo, subtrahend_hi = subtrahend.to_lo_hi()

        borrow_lo = minuend_lo.n < subtrahend_lo.n
        diff_lo = minuend_lo - subtrahend_lo + (1 << 128 if borrow_lo else 0)
        borrow_hi = minuend_hi.n < subtrahend_hi.n + borrow_lo
        diff_hi = minuend_hi - subtrahend_hi - borrow_lo + (1 << 128 if borrow_hi else 0)

        return Word((diff_lo, diff_hi)), FQ(borrow_hi)

    def mul_word_by_u64(self, multiplicand: Word, multiplier: Expression) -> Tuple[Word, FQ]:
        multiplicand_lo, multiplicand_hi = multiplicand.to_lo_hi()

        quotient_lo, product_lo = divmod((multiplicand_lo * multiplier.expr()).n, 1 << 128)
        quotient_hi, product_hi = divmod(
            (multiplicand_hi * multiplier.expr() + quotient_lo).n, 1 << 128
        )

        product_bytes = product_lo.to_bytes(16, "little") + product_hi.to_bytes(16, "little")

        return Word((FQ(product_lo), FQ(product_hi))), FQ(quotient_hi)

    def mul_add_words(self, a: Word, b: Word, c: Word, d: Word) -> FQ:
        """
        The function constrains a * b + c == d, where a, b, c, d are 256-bit words.
        It returns the overflow part of a * b + c.
        """
        a64s = a.to_64s()
        b64s = b.to_64s()
        c_lo, c_hi = c.to_lo_hi()
        d_lo, d_hi = d.to_lo_hi()

        t0 = a64s[0] * b64s[0]
        t1 = a64s[0] * b64s[1] + a64s[1] * b64s[0]
        t2 = a64s[0] * b64s[2] + a64s[1] * b64s[1] + a64s[2] * b64s[0]
        t3 = a64s[0] * b64s[3] + a64s[1] * b64s[2] + a64s[2] * b64s[1] + a64s[3] * b64s[0]
        carry_lo = (t0 + (t1 * 2**64) + c_lo - d_lo) / (2**128)
        carry_hi = (t2 + (t3 * 2**64) + c_hi + carry_lo - d_hi) / (2**128)
        overflow = (
            carry_hi
            + a64s[1] * b64s[3]
            + a64s[2] * b64s[2]
            + a64s[3] * b64s[1]
            + a64s[2] * b64s[3]
            + a64s[3] * b64s[2]
            + a64s[3] * b64s[3]
        )

        # range check for carries
        self.range_check(carry_lo, 9)
        self.range_check(carry_hi, 9)

        self.constrain_equal(t0 + t1 * (2**64) + c_lo, d_lo + carry_lo * (2**128))
        self.constrain_equal(t2 + t3 * (2**64) + c_hi + carry_lo, d_hi + carry_hi * (2**128))

        return overflow

    def mul_add_words_512(self, a: Word, b: Word, c: Word, d: Word, e: Word):
        """
        The function constrains a * b + c == d * 2**256 + e, where a, b, c, d are 256-bit words.
        """
        a64s = a.to_64s()
        b64s = b.to_64s()
        c_lo, c_hi = c.to_lo_hi()
        d_lo, d_hi = d.to_lo_hi()
        e_lo, e_hi = e.to_lo_hi()

        t0 = a64s[0] * b64s[0]
        t1 = a64s[0] * b64s[1] + a64s[1] * b64s[0]
        t2 = a64s[0] * b64s[2] + a64s[1] * b64s[1] + a64s[2] * b64s[0]
        t3 = a64s[0] * b64s[3] + a64s[1] * b64s[2] + a64s[2] * b64s[1] + a64s[3] * b64s[0]

        t4 = a64s[1] * b64s[3] + a64s[2] * b64s[2] + a64s[3] * b64s[1]
        t5 = a64s[2] * b64s[3] + a64s[3] * b64s[2]
        t6 = a64s[3] * b64s[3]

        carry_0 = (t0 + t1 * (2**64) + c_lo - e_lo) / (2**128)
        carry_1 = (t2 + t3 * (2**64) + c_hi + carry_0 - e_hi) / (2**128)
        carry_2 = (t4 + t5 * (2**64) + carry_1 - d_lo) / (2**128)

        # range check for carries
        self.range_check(carry_0, 9)
        self.range_check(carry_1, 9)
        self.range_check(carry_2, 9)

        self.constrain_equal(t0 + t1 * (2**64) + c_lo, e_lo + carry_0 * (2**128))
        self.constrain_equal(t2 + t3 * (2**64) + c_hi + carry_0, e_hi + carry_1 * (2**128))
        self.constrain_equal(t4 + t5 * (2**64) + carry_1, d_lo + carry_2 * (2**128))
        self.constrain_equal(t6 + carry_2, d_hi)

    def fixed_lookup(
        self,
        tag: FixedTableTag,
        value0: Expression,
        value1: Expression = FQ(0),
        value2: Expression = FQ(0),
    ) -> FixedTableRow:
        return self.tables.fixed_lookup(FQ(tag), value0, value1, value2)

    def block_context_lookup(
        self, field_tag: BlockContextFieldTag, block_number: Expression = FQ(0)
    ) -> Expression:
        return self.tables.block_lookup(FQ(field_tag), block_number).value.value()

    def block_context_lookup_word(
        self, field_tag: BlockContextFieldTag, block_number: Expression = FQ(0)
    ) -> WordOrValue:
        return self.tables.block_lookup(FQ(field_tag), block_number).value

    def tx_context_lookup(self, tx_id: Expression, field_tag: TxContextFieldTag) -> Expression:
        return self.tables.tx_lookup(tx_id, FQ(field_tag)).value.value()

    def tx_context_lookup_word(
        self, tx_id: Expression, field_tag: TxContextFieldTag
    ) -> WordOrValue:
        return self.tables.tx_lookup(tx_id, FQ(field_tag)).value

    def tx_calldata_lookup(self, tx_id: Expression, call_data_index: Expression) -> Expression:
        return (
            self.tables.tx_lookup(tx_id, FQ(TxContextFieldTag.CallData), call_data_index)
            .value.value()
            .expr()
        )

    # look up tx log fields (Data, Address, Topic),
    def tx_log_lookup(
        self, tx_id: Expression, log_id: Expression, field_tag: TxLogFieldTag, index: int = 0
    ) -> Expression:
        return self.tx_log_lookup_word(tx_id, log_id, field_tag, index).value()

    # look up tx log fields (Data, Address, Topic),
    def tx_log_lookup_word(
        self, tx_id: Expression, log_id: Expression, field_tag: TxLogFieldTag, index: int = 0
    ) -> WordOrValue:
        # evm only write tx log
        value = self.rw_lookup(
            RW.Write,
            RWTableTag.TxLog,
            id=tx_id,
            address=FQ(index + (int(field_tag) << 32) + (log_id.expr().n << 48)),
            field_tag=FQ(0),
            storage_key=Word(0),
        ).value
        return value

    # look up TxReceipt read for fields (PostStateOrStatus, CumulativeGasUsed, LogLength)
    def tx_receipt_read(
        self,
        tx_id: Expression,
        field_tag: TxReceiptFieldTag,
        rw_counter: Optional[Expression] = None,
    ) -> Expression:
        value = self.rw_lookup(
            RW.Read,
            RWTableTag.TxReceipt,
            id=tx_id,
            address=FQ(0),
            field_tag=FQ(field_tag),
            storage_key=Word(0),
            rw_counter=rw_counter,
        ).value
        return value.value().expr()

    # look up TxReceipt write for fields (PostStateOrStatus, CumulativeGasUsed, LogLength)
    def tx_receipt_write(
        self,
        tx_id: Expression,
        field_tag: TxReceiptFieldTag,
    ) -> Expression:
        value = self.rw_lookup(
            RW.Write,
            RWTableTag.TxReceipt,
            id=tx_id,
            address=FQ(0),
            field_tag=FQ(field_tag),
            storage_key=Word(0),
        ).value
        return value.value().expr()

    # look up byte code value
    def bytecode_lookup(
        self, bytecode_hash: Word, index: Expression, is_code: Optional[Expression] = None
    ) -> Expression:
        return self.tables.bytecode_lookup(
            bytecode_hash, FQ(BytecodeFieldTag.Byte), index, is_code
        ).value

    # lookup value and is_code pair
    def bytecode_lookup_pair(
        self, bytecode_hash: Word, index: Expression
    ) -> Tuple[Expression, Expression]:
        rw = self.tables.bytecode_lookup(bytecode_hash, FQ(BytecodeFieldTag.Byte), index, None)
        return rw.value, rw.is_code

    def bytecode_length(self, bytecode_hash: Word) -> Expression:
        return self.tables.bytecode_lookup(
            bytecode_hash, FQ(BytecodeFieldTag.Header), FQ(0), FQ(0)
        ).value

    def tx_gas_tip_cap(self, tx_id: Expression) -> Word:
        return self.tx_context_lookup_word(tx_id, TxContextFieldTag.GasTipCap)

    def tx_gas_fee_cap(self, tx_id: Expression) -> Word:
        return self.tx_context_lookup_word(tx_id, TxContextFieldTag.GasFeeCap)

    def responsible_opcode_lookup(self, opcode: Expression, aux: Expression = FQ(0)):
        self.fixed_lookup(
            FixedTableTag.ResponsibleOpcode, FQ(self.curr.execution_state), opcode, aux
        )

    def opcode_lookup(self, is_code: bool) -> FQ:
        index = self.curr.program_counter + self.program_counter_offset
        self.program_counter_offset += 1
        return self.opcode_lookup_at(index, is_code)

    def opcode_lookup_at(self, index: FQ, is_code: bool) -> FQ:
        return self.bytecode_lookup(self.curr.code_hash, index, FQ(is_code)).expr()

    def rw_lookup(
        self,
        rw: RW,
        tag: RWTableTag,
        id: Optional[Expression] = None,
        address: Optional[Expression] = None,
        field_tag: Optional[Expression] = None,
        storage_key: Optional[Word] = None,
        value: Optional[Union[Expression, Word]] = None,
        value_prev: Optional[Union[Expression, Word]] = None,
        aux0: Optional[Word] = None,
        rw_counter: Optional[Expression] = None,
    ) -> RWTableRow:
        if rw_counter is None:
            rw_counter = self.curr.rw_counter + self.rw_counter_offset
            self.rw_counter_offset += 1
        if value is not None:
            value = WordOrValue(value)
        if value_prev is not None:
            value_prev = WordOrValue(value_prev)

        return self.tables.rw_lookup(
            rw_counter,
            FQ(rw),
            FQ(tag),
            id,
            address,
            field_tag,
            storage_key,
            value,
            value_prev,
            aux0,
        )

    def state_write(
        self,
        tag: RWTableTag,
        id: Optional[Expression] = None,
        address: Optional[Expression] = None,
        field_tag: Optional[Expression] = None,
        storage_key: Optional[Word] = None,
        value: Optional[Union[Word, Expression]] = None,
        value_prev: Optional[Union[Word, Expression]] = None,
        aux0: Optional[Word] = None,
        reversion_info: Optional[ReversionInfo] = None,
    ) -> RWTableRow:
        assert tag.write_with_reversion()
        if value is not None:
            value = WordOrValue(value)
        if value_prev is not None:
            value_prev = WordOrValue(value_prev)

        row = self.rw_lookup(
            RW.Write, tag, id, address, field_tag, storage_key, value, value_prev, aux0
        )

        if reversion_info is not None and reversion_info.is_persistent == FQ(0):
            self.tables.rw_lookup(
                rw_counter=reversion_info.rw_counter_of_reversion(),
                rw=FQ(RW.Write),
                tag=FQ(tag),
                id=row.id,
                address=row.address,
                field_tag=row.field_tag,
                storage_key=row.storage_key,
                # Swap value and value_prev
                value=row.value_prev,
                value_prev=row.value,
                aux0=row.aux0,
            )

        return row

    def state_read(
        self,
        tag: RWTableTag,
        id: Optional[Expression] = None,
        address: Optional[Expression] = None,
        field_tag: Optional[Expression] = None,
        storage_key: Optional[Word] = None,
        value: Optional[Union[Word, Expression]] = None,
        value_prev: Optional[Union[Word, Expression]] = None,
        aux0: Optional[Word] = None,
    ) -> RWTableRow:
        if value is not None:
            value = WordOrValue(value)
        if value_prev is not None:
            value_prev = WordOrValue(value_prev)
        row = self.rw_lookup(
            RW.Read, tag, id, address, field_tag, storage_key, value, value_prev, aux0
        )
        return row

    def call_context_lookup(
        self, field_tag: CallContextFieldTag, rw: RW = RW.Read, call_id: Optional[Expression] = None
    ) -> Expression:
        return self.call_context_lookup_word(field_tag, rw, call_id).value()

    def call_context_lookup_word(
        self, field_tag: CallContextFieldTag, rw: RW = RW.Read, call_id: Optional[Expression] = None
    ) -> WordOrValue:
        if call_id is None:
            call_id = self.curr.call_id
        return self.rw_lookup(rw, RWTableTag.CallContext, call_id, FQ(field_tag)).value

    def rw_table_start_lookup(self, counter: Expression):
        # Raises exception if no lookup matches
        self.rw_lookup(rw=RW.Read, tag=RWTableTag.Start, rw_counter=counter)

    def reversion_info(self, call_id: Optional[Expression] = None) -> ReversionInfo:
        [rw_counter_end_of_reversion, is_persistent] = [
            self.call_context_lookup(tag, call_id=call_id)
            for tag in [
                CallContextFieldTag.RwCounterEndOfReversion,
                CallContextFieldTag.IsPersistent,
            ]
        ]
        return ReversionInfo(
            rw_counter_end_of_reversion,
            is_persistent,
            self.curr.reversible_write_counter if call_id is None else FQ(0),
        )

    def stack_pop(self) -> Word:
        stack_pointer_offset = self.stack_pointer_offset
        self.stack_pointer_offset += 1
        return self.stack_lookup(RW.Read, FQ(stack_pointer_offset))

    def stack_push(self) -> Word:
        self.stack_pointer_offset -= 1
        return self.stack_lookup(RW.Write, FQ(self.stack_pointer_offset))

    def stack_lookup(self, rw: RW, stack_pointer_offset: Expression) -> Word:
        stack_pointer = self.curr.stack_pointer + stack_pointer_offset
        return self.rw_lookup(rw, RWTableTag.Stack, self.curr.call_id, stack_pointer).value

    def memory_lookup(
        self, rw: RW, memory_address: Expression, call_id: Optional[Expression] = None
    ) -> FQ:
        if call_id is None:
            call_id = self.curr.call_id
        return cast_expr(
            self.rw_lookup(rw, RWTableTag.Memory, call_id, memory_address).value.value(), FQ
        )

    def tx_refund_read(self, tx_id: Expression) -> FQ:
        return cast_expr(self.rw_lookup(RW.Read, RWTableTag.TxRefund, tx_id).value.value(), FQ)

    def tx_refund_write(
        self,
        tx_id: Expression,
        reversion_info: Optional[ReversionInfo] = None,
    ) -> Tuple[FQ, FQ]:
        row = self.state_write(
            RWTableTag.TxRefund,
            tx_id,
            reversion_info=reversion_info,
        )
        return cast_expr(row.value.value(), FQ), cast_expr(row.value_prev.value(), FQ)

    def account_read(
        self, account_address: Expression, account_field_tag: AccountFieldTag
    ) -> Expression:
        self.account_read_word(account_address, account_field_tag).value()

    def account_read_word(
        self, account_address: Expression, account_field_tag: AccountFieldTag
    ) -> WordOrValue:
        return self.rw_lookup(
            RW.Read, RWTableTag.Account, address=account_address, field_tag=FQ(account_field_tag)
        ).value

    def account_write(
        self,
        account_address: Expression,
        account_field_tag: AccountFieldTag,
        reversion_info: Optional[ReversionInfo] = None,
    ) -> Tuple[Expression, Expression]:
        pair = self.account_write_word(account_address, account_field_tag, reversion_info)
        return pair[0].value(), pair[1].value()

    def account_write_word(
        self,
        account_address: Expression,
        account_field_tag: AccountFieldTag,
        reversion_info: Optional[ReversionInfo] = None,
    ) -> Tuple[WordOrValue, WordOrValue]:
        row = self.state_write(
            RWTableTag.Account,
            address=account_address,
            field_tag=FQ(account_field_tag),
            reversion_info=reversion_info,
        )
        return row.value, row.value_prev

    def add_balance(
        self,
        account_address: Expression,
        values: Sequence[Word],
        reversion_info: Optional[ReversionInfo] = None,
    ) -> Tuple[Word, Word]:
        balance, balance_prev = self.account_write_word(
            account_address, AccountFieldTag.Balance, reversion_info
        )
        result, carry = self.add_words([balance_prev, *values])
        self.constrain_equal_word(balance, result)
        self.constrain_zero(carry)
        return balance, balance_prev

    def sub_balance(
        self,
        account_address: Expression,
        values: Sequence[Word],
        reversion_info: Optional[ReversionInfo] = None,
    ) -> Tuple[Word, Word]:
        balance, balance_prev = self.account_write_word(
            account_address, AccountFieldTag.Balance, reversion_info
        )
        result, carry = self.add_words([balance, *values])
        self.constrain_equal_word(balance_prev, result)
        self.constrain_zero(carry)
        return balance, balance_prev

    def account_storage_read(
        self, account_address: Expression, storage_key: Word, tx_id: Expression
    ) -> Word:
        row = self.rw_lookup(
            RW.Read,
            RWTableTag.AccountStorage,
            tx_id,
            account_address,
            field_tag=None,
            storage_key=storage_key,
        )
        return row.value

    def account_storage_write(
        self,
        account_address: Expression,
        storage_key: Word,
        tx_id: Expression,
        reversion_info: Optional[ReversionInfo] = None,
    ) -> Tuple[Word, Word, Word]:
        row = self.state_write(
            RWTableTag.AccountStorage,
            tx_id,
            account_address,
            storage_key=storage_key,
            reversion_info=reversion_info,
        )
        return row.value, row.value_prev, row.aux0

    def add_account_to_access_list(
        self,
        tx_id: Expression,
        account_address: Expression,
        reversion_info: Optional[ReversionInfo] = None,
    ) -> FQ:
        row = self.state_write(
            RWTableTag.TxAccessListAccount,
            tx_id,
            account_address,
            value=FQ(1),
            reversion_info=reversion_info,
        )
        return row.value_prev.value().expr()

    def read_account_to_access_list(
        self,
        tx_id: Expression,
        account_address: Expression,
    ) -> FQ:
        row = self.state_read(
            RWTableTag.TxAccessListAccount,
            tx_id,
            account_address,
        )
        return row.value_prev.value().expr()

    def add_account_storage_to_access_list(
        self,
        tx_id: Expression,
        account_address: Expression,
        storage_key: Word,
        reversion_info: Optional[ReversionInfo] = None,
    ) -> FQ:
        row = self.state_write(
            RWTableTag.TxAccessListAccountStorage,
            tx_id,
            account_address,
            storage_key=storage_key,
            value=FQ(1),
            reversion_info=reversion_info,
        )
        return row.value_prev.value().expr()

    def transfer_with_gas_fee(
        self,
        sender_address: Expression,
        receiver_address: Expression,
        value: Word,
        gas_fee: Word,
        reversion_info: Optional[ReversionInfo] = None,
    ) -> Tuple[Tuple[Word, Word], Tuple[Word, Word]]:
        sender_balance_pair = self.sub_balance(sender_address, [value, gas_fee], reversion_info)
        receiver_balance_pair = self.add_balance(receiver_address, [value], reversion_info)
        return sender_balance_pair, receiver_balance_pair

    def transfer(
        self,
        sender_address: Expression,
        receiver_address: Expression,
        value: Word,
        reversion_info: Optional[ReversionInfo] = None,
    ) -> Tuple[Tuple[Word, Word], Tuple[Word, Word]]:
        sender_balance_pair = self.sub_balance(sender_address, [value], reversion_info)
        receiver_balance_pair = self.add_balance(receiver_address, [value], reversion_info)
        return sender_balance_pair, receiver_balance_pair

    def memory_offset_and_length(self, offset_word: Word, length_word: Word) -> Tuple[FQ, FQ]:
        length = self.word_to_fq(length_word, N_BYTES_MEMORY_ADDRESS)
        if self.is_zero(length) == 1:
            return FQ(0), FQ(0)
        offset = self.word_to_fq(offset_word, N_BYTES_MEMORY_ADDRESS)
        return offset, length

    def memory_gas_cost(self, memory_size: Expression) -> FQ:
        quadratic_cost, _ = self.constant_divmod(
            memory_size.expr() * memory_size.expr(),
            FQ(MEMORY_EXPANSION_QUAD_DENOMINATOR),
            N_BYTES_GAS,
        )
        linear_cost = memory_size.expr() * MEMORY_EXPANSION_LINEAR_COEFF
        return quadratic_cost + linear_cost

    def memory_expansion(self, offset: Expression, length: Expression) -> Tuple[FQ, FQ]:
        memory_size, _ = self.constant_divmod(
            length.expr() + offset.expr() + 31, FQ(32), N_BYTES_MEMORY_SIZE
        )

        next_memory_size = self.max(self.curr.memory_word_size, memory_size, N_BYTES_MEMORY_SIZE)

        memory_gas_cost = self.memory_gas_cost(self.curr.memory_word_size)
        memory_gas_cost_next = self.memory_gas_cost(next_memory_size)
        memory_expansion_gas_cost = memory_gas_cost_next - memory_gas_cost

        return cast_expr(next_memory_size, FQ), cast_expr(memory_expansion_gas_cost, FQ)

    def memory_expansion_dynamic_length(
        self,
        cd_offset: Expression,
        cd_length: Expression,
        rd_offset: Optional[Expression] = None,
        rd_length: Optional[Expression] = None,
    ) -> Tuple[FQ, FQ]:
        cd_memory_size, _ = self.constant_divmod(
            cd_offset.expr() + cd_length.expr() + FQ(31), FQ(32), N_BYTES_MEMORY_SIZE
        )
        next_memory_size = self.max(self.curr.memory_word_size, cd_memory_size, N_BYTES_MEMORY_SIZE)

        if rd_offset is not None and rd_length is not None:
            rd_memory_size, _ = self.constant_divmod(
                rd_offset.expr() + rd_length.expr() + FQ(31), FQ(32), N_BYTES_MEMORY_SIZE
            )
            next_memory_size = self.max(next_memory_size, rd_memory_size, N_BYTES_MEMORY_SIZE)

        memory_gas_cost = self.memory_gas_cost(self.curr.memory_word_size)
        memory_gas_cost_next = self.memory_gas_cost(next_memory_size)
        memory_expansion_gas_cost = memory_gas_cost_next - memory_gas_cost

        return cast_expr(next_memory_size, FQ), cast_expr(memory_expansion_gas_cost, FQ)

    def memory_copier_gas_cost(
        self,
        length: Expression,
        memory_expansion_gas_cost: Expression,
        gas_cost_copy: int = GAS_COST_COPY,
    ) -> FQ:
        word_size, _ = self.constant_divmod(length + FQ(31), FQ(32), N_BYTES_MEMORY_SIZE)
        gas_cost = word_size * gas_cost_copy + memory_expansion_gas_cost
        self.range_check(gas_cost, N_BYTES_GAS)
        return gas_cost

    def generate_contract_address(self, address: Expression, nonce: Expression) -> Expression:
        contract_addr = keccak(rlp.encode([address.expr().n.to_bytes(20, "big"), nonce.expr().n]))
        return FQ(int.from_bytes(contract_addr[-20:], "big"))

    def generate_CREAET2_contract_address(
        self, address: Expression, salt: Word, code_hash: Word
    ) -> Expression:
        # keccak256(0xff + sender_address + salt + keccak256(initialisation_code))[12:]
        contract_addr = keccak(
            b"\xff"
            + address.expr().n.to_bytes(20, "big")
            + salt.int_value().to_bytes(32, "little")
            + code_hash.int_value().to_bytes(32, "little")
        )
        return FQ(int.from_bytes(contract_addr[-20:], "big"))

    def pow2_lookup(self, value: Expression, pow_lo128: Expression, pow_hi128: Expression):
        self.fixed_lookup(FixedTableTag.Pow2, value, pow_lo128, pow_hi128)

    # `sign_byte == 0xff` if (value as i8 < 0) otherwise `sign_byte == 0`.
    def sign_byte_lookup(self, value: Expression, sign_byte: Expression):
        self.fixed_lookup(FixedTableTag.SignByte, value, sign_byte, FQ(0))

    def copy_lookup(
        self,
        src_id: Union[Expression, Word],
        src_tag: CopyDataTypeTag,
        dst_id: Union[Expression, Word],
        dst_tag: CopyDataTypeTag,
        src_addr: Expression,
        src_addr_end: Expression,
        dst_addr: Expression,
        length: Expression,
        rw_counter: Expression,
        log_id: Optional[Expression] = None,
    ) -> Tuple[FQ, FQ]:
        copy_table_row = self.tables.copy_lookup(
            src_id,
            FQ(src_tag),
            dst_id,
            FQ(dst_tag),
            src_addr,
            src_addr_end,
            dst_addr,
            length,
            rw_counter,
            log_id,
        )
        return copy_table_row.rwc_inc, copy_table_row.rlc_acc

    def keccak_lookup(self, length: Expression, value_rlc: Expression) -> Word:
        return self.tables.keccak_lookup(length, value_rlc).output

    def exp_lookup(
        self,
        identifier: Expression,
        is_last: Expression,
        base_limbs: Tuple[Expression, ...],
        exponent: Word,
    ) -> Word:
        exp_table_row = self.tables.exp_lookup(identifier, is_last, base_limbs, exponent)
        return exp_table_row.exponentiation

    def constrain_error_state(self, rw_counter_delta: int):
        # Current call must fail.
        is_success = self.call_context_lookup(CallContextFieldTag.IsSuccess)
        self.constrain_equal(is_success, FQ(0))

        # Go to EndTx only when is_root.
        is_to_end_tx = self.is_equal(self.next.execution_state, ExecutionState.EndTx)
        self.constrain_equal(FQ(self.curr.is_root), is_to_end_tx)

        # When it's a root call.
        if self.curr.is_root:
            # Do step state transition.
            self.constrain_step_state_transition(
                rw_counter=Transition.delta(rw_counter_delta),
                call_id=Transition.same(),
            )
        else:
            # When it is internal call, need to restore caller's state as finishing this call.
            # Restore caller state to next StepState.
            self.step_state_transition_to_restored_context(
                rw_counter_delta=rw_counter_delta,
                return_data_offset=FQ(0),
                return_data_length=FQ(0),
                # Consume all gas_left if it's an exception step.
                gas_left=FQ(0),
            )
