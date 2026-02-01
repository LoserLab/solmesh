"""Shared constants for the SolMesh protocol."""

import struct

# Protocol
MAGIC = b"\x53\x4d"  # "SM"
PROTOCOL_VERSION = 1
HEADER_SIZE = 10
HEADER_FORMAT = "!2sBBHBBBB"  # big-endian: magic(2) version(1) type(1) id(2) chunk(1) total(1) len(1) crc(1)

# LoRa payload limits
MAX_LORA_PAYLOAD = 233  # Meshtastic DATA_PAYLOAD_LEN
SAFE_LORA_PAYLOAD = 220  # conservative limit
MAX_CHUNK_DATA = SAFE_LORA_PAYLOAD - HEADER_SIZE  # 210 bytes per chunk

# Timeouts and retries
CHUNK_REASSEMBLY_TIMEOUT = 120  # seconds
ACK_TIMEOUT = 30  # seconds
MAX_RETRIES = 3
RETRY_DELAY = 10  # seconds
INTER_CHUNK_DELAY = 2.0  # seconds between chunks
MAX_FLUSH_ATTEMPTS = 3  # max end-to-end send attempts per queued intent

# Solana
LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
DEVNET_RPC_URL = "https://api.devnet.solana.com"
TESTNET_RPC_URL = "https://api.testnet.solana.com"

# SPL Token constants
USDC_MINT_MAINNET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_MINT_DEVNET = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
USDC_DECIMALS = 6
USDC_BASE_UNITS_PER_TOKEN = 10 ** USDC_DECIMALS  # 1_000_000

FXN_MINT_MAINNET = "92cRC6kV5D7TiHX1j56AbkPbffo9jwcXxSDQZ8Mopump"
FXN_DECIMALS = 6

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

# Known token registry: mint address -> (symbol, decimals)
KNOWN_TOKENS = {
    USDC_MINT_MAINNET: ("USDC", 6),
    USDC_MINT_DEVNET: ("USDC", 6),
    FXN_MINT_MAINNET: ("FXN", 6),
}


class MsgType:
    """SolMesh message types (1 byte)."""

    TX_CHUNK = 0x01  # Chunk of a serialized signed transaction
    TX_REQUEST = 0x02  # Request gateway to create & send a transfer
    ADDR_SHARE = 0x03  # Share a Solana public address
    ACK = 0x10  # Acknowledgment
    NACK = 0x11  # Negative acknowledgment
    BALANCE_REQ = 0x20  # Request SOL balance
    BALANCE_RESP = 0x21  # Balance response
    BLOCKHASH_REQ = 0x22  # Request recent blockhash
    BLOCKHASH_RESP = 0x23  # Recent blockhash response
    TX_RESULT = 0x30  # Transaction result (signature or error)
    GATEWAY_BEACON = 0x40  # Gateway presence beacon


class ErrorCode:
    """Error codes for NACK messages."""

    UNKNOWN = 0x00
    CHECKSUM_FAIL = 0x01
    REASSEMBLY_TIMEOUT = 0x02
    INVALID_TX = 0x03
    RPC_ERROR = 0x04
    UNAUTHORIZED = 0x05
    AMOUNT_EXCEEDED = 0x06
    INSUFFICIENT_BALANCE = 0x07
    RATE_LIMITED = 0x08
    UNSUPPORTED_TOKEN = 0x09
