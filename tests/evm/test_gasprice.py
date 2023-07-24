import pytest

from zkevm_specs.evm_circuit import (
    Block,
    Bytecode,
    CallContextFieldTag,
    ExecutionState,
    StepState,
    Tables,
    Transaction,
    verify_steps,
    RWDictionary,
)
from zkevm_specs.util import Word, U256

TESTING_DATA = (
    0x00,
    0x10,
    0x302010,
    0xF0FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF0F,
)


@pytest.mark.parametrize("gasprice", TESTING_DATA)
def test_gasprice(gasprice: U256):
    tx = Transaction(gas_fee_cap=gasprice)

    bytecode = Bytecode().gasprice().stop()
    bytecode_hash = Word(bytecode.hash())

    tables = Tables(
        block_table=set(Block().table_assignments()),
        tx_table=set(tx.table_assignments()),
        bytecode_table=set(bytecode.table_assignments()),
        rw_table=set(
            RWDictionary(9)
            .call_context_read(1, CallContextFieldTag.TxId, tx.id)
            .stack_write(1, 1023, Word(gasprice))
            .rws
        ),
    )

    verify_steps(
        tables=tables,
        steps=[
            StepState(
                execution_state=ExecutionState.GASPRICE,
                rw_counter=9,
                call_id=1,
                is_root=True,
                is_create=False,
                code_hash=bytecode_hash,
                program_counter=0,
                stack_pointer=1024,
                gas_left=2,
            ),
            StepState(
                execution_state=ExecutionState.STOP,
                rw_counter=11,
                call_id=1,
                is_root=True,
                is_create=False,
                code_hash=bytecode_hash,
                program_counter=1,
                stack_pointer=1023,
                gas_left=0,
            ),
        ],
    )
