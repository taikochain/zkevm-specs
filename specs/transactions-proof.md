# Transactions proof

The transactions proof verifies each transaction signature, that the merkle
patricia trie identified by the root `transactionsRoot` contains all the
transactions (and no more), and makes the transactions data easily accessible
to the EVM proof via the transactions table.

## Transaction encoding

Different types of transaction encoding exist.  On the first iteration of the zkEVM we will only support Legacy transactions with EIP-155.  We plan to add support for Non-Legacy (EIP-2718) transactions later.

### Legacy type:

```
rlp([nonce, gasPrice, gas, to, value, data, sig_v, r, s])
```

Before EIP-155:

Hashed data to sign: `(nonce, gasprice, gas, to, value, data)` with `sig_v = {0,1} + 27`

After EIP-155:

Hashed data to sign: `(nonce, gasprice, gas, to, value, data, chain_id, 0, 0)` with `sig_v = {0,1} + CHAIN_ID * 2 + 35`

Where `{0,1}` is the parity of the `y` value of the curve point corresponding to the public key in the secp256k1 signing process.

### Non-Legacy (EIP-2718) type:

From https://eips.ethereum.org/EIPS/eip-1559 and https://eips.ethereum.org/EIPS/eip-2718

```
0x02 || rlp([chain_id, nonce, max_priority_fee_per_gas, max_fee_per_gas, gas, destination, amount, data, access_list, signature_y_parity, signature_r, signature_s])
```

Hashed data to sign: TODO

## Circuit behaviour

Using the following public inputs: `chain_id`, `transactionsRoot`.

For every transaction defined as the parameters `(nonce, gas_price, gas, to, value, data, sig_v, sig_r, sig_s)` and using as public inputs `(nonce, gas_price, gas, to, value, data, from)`, the circuit verifies the following:

1. `txSignData: bytes = rlp([nonce, gas_price, gas, to, value, data, chain_id, 0, 0])`
2. `txSignHash: word = keccak(txSignData)`
3. `sig_parity: {0, 1} = sig_v - 35 - chain_id / 2`
4. `ecdsa_recover(txSignHash, sig_parity, sig_r, sig_s) = pubKey` or equivalently `verify(txSignHash, sig_r, sig_s, pubKey) = true`
5. `fromAddress = keccak(pubKey)[-20:]`

- The rlp encoding of transaction parameters (step 1) will be done using a
  custom rlp encoding gadget, isolated from the rlp encoding used by the MPT
  circuit.
- The signature message keccak hash verification (step 2) will be done in the
  keccak circuit; the tx circuit will do a single lookup (with the rlp encoded
  transaction accumulated into a single value using RLC) to the keccak table.
- The public key recovery from the message and signature (step 3) will be done
  in the ECDSA circuit; the tx circuit will do a lookup to the ECDSA table.
- The public key keccak hash verification (step 5) will be done in the keccak
  circuit; the tx circuit will do a lookup to the keccak table.

From this information the circuit builds the TxTable:

Where:

- Gas = gas
- GasTipCap = 0
- GasFeeCap = 0
- CallerAddress = fromAddress
- CalleeAddress = to
- IsCreate = `1 if to is None else 0`
- CallDataLength = len(data)
- CallData\[$ByteIndex\] = data\[$ByteIndex\]

| 0 TxID | 1 Tag               | 2 Index    | 3 value     |
| ---    | ---                 | ---        | ---         |
|        | *TxContextFieldTag* |            |             |
| $TxID  | Nonce               | 0          | $value: raw |
| $TxID  | Gas                 | 0          | $value: raw |
| $TxID  | GasPrice            | 0          | $value: rlc |
| $TxID  | GasTipCap           | 0          | $value: 0   |
| $TxID  | GasFeeCap           | 0          | $value: 0   |
| $TxID  | CallerAddress       | 0          | $value: raw |
| $TxID  | CalleeAddress       | 0          | $value: raw |
| $TxID  | IsCreate            | 0          | $value: raw |
| $TxID  | Value               | 0          | $value: rlc |
| $TxID  | CallDataLength      | 0          | $value: raw |
| $TxID  | CallData            | $ByteIndex | $value: raw |

There are some constraints on the shape of the table like:

- For every Tx, each tag must appear exactly once, except for `CallData` which can be repeated but only with sequential `ByteIndex` starting at 0.
- `TxID` must start at 1 and increase sequentially for each transaction
- When `Tag` is `CallData`, value must be between 0 and 255

Since the transaction table is built from public inputs, the verifier (the L1
smart contract in the zkRollup for example) needs to validate that all rows of
the table are properly built using the transaction data.  Since the table
construction is validated outside of the circuit, there's no need to verify the
same constraints inside of the circuit.

### Transaction Trie

For each transaction, the tx circuit also must prepare the key and value used
to build the transaction trie.  These keys and values are used in lookups to
the MPT table in order to verify that a tree built with the key-values
corresponding to the transactions has the root value `transactionsRoot`.

> By doing lookups to the MPT table, we prove that when we start with an empty
> MPT, and do a chain of key-value insertions corresponding to each
> transaction, we reach a Trie with root value `transactionsRoot`.

Each MPT update uses the following parameters:

- Key = `rlp(tx_index)`
- Value = `rlp([nonce, gas_price, gas, to, value, data, sig_v, r, s])`
- ValuePrev = `0`

NOTE: The shape of the MPT lookup table for transaction trie update entries is not
yet defined.

NOTE: The MPT proof used for the Transaction Trie doesn't need deletion support.

`go-ethereum` reference:

- [Transaction Trie Root calculation](https://github.com/ethereum/go-ethereum/blob/70da74e73a182620a09bb0cfbff173e6d65d0518/core/types/hashing.go#L86)
- [Transaction RLP encoding](https://github.com/ethereum/go-ethereum/blob/70da74e73a182620a09bb0cfbff173e6d65d0518/core/types/transaction.go#L405)

## Circuit Behaviour shortcut 1

For the first implementation of the transaction circuit we will apply
some shortcut as a simplification.  For each transaction, the following values
will be provided as valid public inputs (and won't be verified in the circuit):

- `txSignHash` (this implies that the circuit doesn't need to calculate `txSignData`).

In particular, for the zkRollup, we will calculate the `txSignData` and
`txSignHash` in the L1 contract as part of the verification process.

We will also skip the verification of correctly constructing the Transaction
Trie.  Currently the MPT circuit is being specified and implemented for the
need of the Account Storage Tries and State Trie updates, which implies some
difference in usage compared to the Tx Circuit:

1. The lookup table needs to be defined in the MPT for these Tx Circuit
   particular lookups (which are separate from the State Trie and Account
   Storage Lookups).  Here we're building a trie from scratch and getting its
   root.
2. While the State Trie and Account Storage Trie inserts use leafs that are
   bounded in size, for the Transactions Trie, the leafs are the RLP of the
   Transaction, which contains a variable size calldata.  This means that the
   MPT circuit needs to accommodate variable length leaf values.

Once the first iteration of the MPT (the one that fulfills the needs of the
State Circuit lookups) is finished, we'll work on this.

For this implementation, the Tx Table is extended to look like this:

| 0 TxID | 1 Tag               | 2 Index    | 3 value     |
| ---    | ---                 | ---        | ---         |
|        | *TxContextFieldTag* |            |             |
| $TxID  | Nonce               | 0          | $value: raw |
| $TxID  | Gas                 | 0          | $value: raw |
| $TxID  | GasPrice            | 0          | $value: rlc |
| $TxID  | GasTipCap           | 0          | $value: 0   |
| $TxID  | GasFeeCap           | 0          | $value: 0   |
| $TxID  | CallerAddress       | 0          | $value: raw |
| $TxID  | CalleeAddress       | 0          | $value: raw |
| $TxID  | IsCreate            | 0          | $value: raw |
| $TxID  | Value               | 0          | $value: rlc |
| $TxID  | CallDataLength      | 0          | $value: raw |
| $TxID  | TxSignHash          |            | $value: rlc |
| $TxID  | CallData            | $ByteIndex | $value: raw |

For the ECDSA signature verification, instead of doing lookups to the ECDSA table we'll just use an ECDSA verification gadget for each transaction.  Since each transaction uses a variable number of rows due to the variable length CallData, we'll rearrange the table so that each transaction starts at a fixed offset like this (by moving all the CallData rows at the end):

For each transaction:

| 0 TxID | 1 Tag               | 2 Index    | 3 value     |
| ---    | ---                 | ---        | ---         |
|        | *TxContextFieldTag* |            |             |
| $TxID  | Nonce               | 0          | $value: raw |
| $TxID  | Gas                 | 0          | $value: raw |
| $TxID  | GasPrice            | 0          | $value: rlc |
| $TxID  | GasTipCap           | 0          | $value: 0   |
| $TxID  | GasFeeCap           | 0          | $value: 0   |
| $TxID  | CallerAddress       | 0          | $value: raw |
| $TxID  | CalleeAddress       | 0          | $value: raw |
| $TxID  | IsCreate            | 0          | $value: raw |
| $TxID  | Value               | 0          | $value: rlc |
| $TxID  | CallDataLength      | 0          | $value: raw |
| $TxID  | TxSignHash          |            | $value: rlc |

This structure is repeated `MAX_TXS` times.  When the number of transactions is
less than `MAX_TXS`, the rows corresponding to unused transactions will use the
continue having sequential `TxID`s but will have all values set to 0.  In particular, the signature verification is disabled when `CallerAddress == 0`.

Then the table continues: for each transaction:

| 0 TxID | 1 Tag               | 2 Index    | 3 value     |
| ---    | ---                 | ---        | ---         |
|        | *TxContextFieldTag* |            |             |
| $TxID  | CallData            | $ByteIndex | $value: raw |

These rows are repeated `MAX_CALLDATA_BYTES` times.  When the total number of
bytes from all transactions' call data is less than `MAX_CALLDATA_BYTES`, the
rows corresponding to unused transactions will use the `CallData` tag with
`TxID = 0`.

Organizing the table this way allows having the values of `CallerAddress` and
`TxSignHash` for each transaction at a fixed offset.  This makes it possible to
add copy constraints of these values to cells into another region that performs
the signature verification and hash lookup.

### Diagram

![](./tx_circuit.rev1.png)

### Signature verification and random linear combination

Instead of verifying ECDSA signatures by doing lookups to an ECDSA table, we use ECDSA chip from `halo2wrong` directly.

We also extend the `MainGate`, which is the underlying component of ECDSA chip, to build the random linear combination of these data:

- PublicKey - Decomposed from signature verification and then used to lookup to keccak table as input
- PublicKeyHash - Used to lookup to keccak table as output
- TxSignHash - Decomposed from signature verification and then used to copy to tx table

The extended random linear combination gate requires one extra column in second phase to contain the running sum of RLC, which accumulate inputs chunk by chunk and finally build the RLC of the whole input.

### Summary of changes

- Skip verification of correct construction of the transaction trie (no MPT table lookups)
- Skip verification of RLC of transaction to obtain the message to sign
- Skip verification of hash of RLC of transaction to obtain the hash of the message to sign
- Add TxSignHash Tag to the tx table (set from public input calculated in L1 smart contract)
- Instead of verifying ECDSA signatures by doing lookups to an ECDSA table, use ECDSA chip directly.
  - This requires rearangement of the tx table so that for each transaction we
    can find its CallerAddress Value and TxSignHash value at fixed offset to do
    a copy constraint to a signature verification gadget.  For this we move the
    CallData tags of all transactions to the end of the table, and we define
    padding at the middle (between the fixed offset region and dynamic offset
    region) and at the end.

## Code

Please refer to `src/zkevm-specs/tx_circuit.py`.
