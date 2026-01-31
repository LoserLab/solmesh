"""SPL Token instruction builders for SolMesh.

Builds token transfer and ATA creation instructions using solders primitives.
No additional dependencies required beyond solders.
"""

from __future__ import annotations

import struct

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.hash import Hash as SolHash
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from solmesh.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID

# Well-known program pubkeys
SPL_TOKEN_PROGRAM = Pubkey.from_string(TOKEN_PROGRAM_ID)
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")


def find_associated_token_address(wallet: Pubkey, mint: Pubkey) -> Pubkey:
    """Derive the associated token account (ATA) address for a wallet and mint."""
    ata, _bump = Pubkey.find_program_address(
        [bytes(wallet), bytes(SPL_TOKEN_PROGRAM), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )
    return ata


def create_associated_token_account_instruction(
    payer: Pubkey,
    wallet: Pubkey,
    mint: Pubkey,
) -> Instruction:
    """Build a create-associated-token-account instruction."""
    ata = find_associated_token_address(wallet, mint)

    return Instruction(
        program_id=ASSOCIATED_TOKEN_PROGRAM,
        accounts=[
            AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
            AccountMeta(pubkey=ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=wallet, is_signer=False, is_writable=False),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SPL_TOKEN_PROGRAM, is_signer=False, is_writable=False),
        ],
        data=b"",
    )


def transfer_checked_instruction(
    source_ata: Pubkey,
    mint: Pubkey,
    dest_ata: Pubkey,
    owner: Pubkey,
    amount: int,
    decimals: int,
) -> Instruction:
    """Build an SPL Token TransferChecked instruction.

    TransferChecked (instruction index 12) layout:
      - 1 byte: instruction discriminator (12)
      - 8 bytes: amount (little-endian u64)
      - 1 byte: decimals
    """
    data = struct.pack("<BQB", 12, amount, decimals)

    return Instruction(
        program_id=SPL_TOKEN_PROGRAM,
        accounts=[
            AccountMeta(pubkey=source_ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            AccountMeta(pubkey=dest_ata, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
        ],
        data=data,
    )


def create_spl_transfer(
    sender_keypair: Keypair,
    recipient_pubkey: Pubkey,
    mint: Pubkey,
    amount: int,
    decimals: int,
    recent_blockhash: SolHash,
    create_recipient_ata: bool = False,
) -> bytes:
    """Create, sign, and serialize an SPL token transfer transaction.

    If create_recipient_ata is True, includes a create-ATA instruction
    before the transfer (for when the recipient has no token account).

    Returns raw serialized bytes ready for chunking.
    Private key never leaves this function.
    """
    sender_ata = find_associated_token_address(sender_keypair.pubkey(), mint)
    recipient_ata = find_associated_token_address(recipient_pubkey, mint)

    instructions = []

    if create_recipient_ata:
        create_ix = create_associated_token_account_instruction(
            payer=sender_keypair.pubkey(),
            wallet=recipient_pubkey,
            mint=mint,
        )
        instructions.append(create_ix)

    transfer_ix = transfer_checked_instruction(
        source_ata=sender_ata,
        mint=mint,
        dest_ata=recipient_ata,
        owner=sender_keypair.pubkey(),
        amount=amount,
        decimals=decimals,
    )
    instructions.append(transfer_ix)

    msg = MessageV0.try_compile(
        payer=sender_keypair.pubkey(),
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=recent_blockhash,
    )
    tx = VersionedTransaction(msg, [sender_keypair])
    return bytes(tx)
