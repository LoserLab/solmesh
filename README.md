# SolMesh

Send Solana transactions over Meshtastic/LoRa mesh networks.

SolMesh enables cryptocurrency transfers in off-grid environments using LoRa radio. Supports native SOL and SPL tokens (USDC, FXN). Transactions are signed locally (private keys never leave your device), chunked to fit within LoRa's bandwidth constraints, and relayed through a gateway node to the Solana network.

## Operating Modes

### Mode 1: Offline Sign + Relay
Sign a Solana transaction on your local device, send the signed transaction over the mesh to an internet-connected gateway that broadcasts it to Solana. The gateway can provide a fresh blockhash on request, so your transaction won't expire before it's submitted.

### Mode 2: Wallet-to-Wallet
Exchange Solana addresses with other mesh nodes over LoRa. Address sharing includes ACK-based delivery confirmation with automatic retry.

### Mode 3: Full Gateway
A gateway node holds a hot wallet. Remote offline nodes send authenticated transfer requests (SOL or SPL tokens), and the gateway signs and broadcasts on their behalf. Requests are authorized by Ed25519 signature verification against a Solana pubkey allowlist.

## Installation

```bash
pip install -e .
```

Or with dev dependencies:

```bash
pip install -e ".[dev]"
```

## Quick Start

### 1. Create a wallet

```bash
solmesh wallet create --name mywallet
```

This generates a BIP39 mnemonic (24 words) and derives a Solana keypair. The mnemonic is displayed once -- write it down and store it safely. The mnemonic is **not** stored on disk.

To create a wallet without a mnemonic:

```bash
solmesh wallet create --name mywallet --no-mnemonic
```

### 2. Recover a wallet from mnemonic

```bash
solmesh wallet recover --name restored --mnemonic "word1 word2 ... word24"
```

### 3. Run a gateway (internet-connected node)

```bash
solmesh gateway --rpc-url https://api.devnet.solana.com
```

The gateway broadcasts periodic beacons so clients can auto-discover it. Configure the beacon interval:

```bash
solmesh gateway --rpc-url https://api.devnet.solana.com --beacon-interval 120
```

### 4. Send SOL (offline node)

**Mode 1** - Sign locally and relay:
```bash
solmesh send relay \
  --wallet mywallet \
  --to <RECIPIENT_ADDRESS> \
  --amount 0.5 \
  --gateway-node '!aabbccdd'
```

The blockhash is fetched automatically from the gateway. You can also provide one explicitly with `--blockhash <RECENT_BLOCKHASH>`.

With gateway auto-discovery (no need to know the gateway node ID):
```bash
solmesh send relay \
  --wallet mywallet \
  --to <RECIPIENT_ADDRESS> \
  --amount 0.5 \
  --auto-discover
```

**Mode 3** - Request gateway to send from its hot wallet:
```bash
solmesh send request \
  --wallet mywallet \
  --to <RECIPIENT_ADDRESS> \
  --amount 0.1 \
  --gateway-node '!aabbccdd'
```

### 5. Send USDC or SPL tokens

All send commands accept a `--token` flag. Use `USDC` or `FXN` as shorthands, or pass any SPL token mint address directly. Note: FXN is mainnet only.

**Mode 1** - Sign a USDC transfer locally and relay:
```bash
solmesh send relay \
  --wallet mywallet \
  --to <RECIPIENT_ADDRESS> \
  --amount 5.0 \
  --token USDC \
  --gateway-node '!aabbccdd'
```

If the recipient doesn't have an associated token account yet, add `--create-ata` to have the transaction create one.

**Mode 3** - Request USDC transfer from the gateway hot wallet:
```bash
solmesh send request \
  --wallet mywallet \
  --to <RECIPIENT_ADDRESS> \
  --amount 5.0 \
  --token USDC \
  --gateway-node '!aabbccdd'
```

SOL commands are unchanged -- omit `--token` to send SOL.

### 6. Check balance

```bash
solmesh balance --address <SOLANA_ADDRESS> --gateway-node '!aabbccdd'
```

Or with auto-discovery:

```bash
solmesh balance --address <SOLANA_ADDRESS> --auto-discover
```

Check a token balance:

```bash
solmesh balance --address <SOLANA_ADDRESS> --token USDC --gateway-node '!aabbccdd'
```

### 7. Share your address

```bash
solmesh share-address --wallet mywallet --label "Field Node Alpha"
```

### 8. Store-and-Forward (Deferred Transactions)

Queue transactions when no gateway is available, flush them when connectivity returns.

**Queue a transaction (no mesh connection needed):**
```bash
solmesh send deferred \
  --wallet mywallet \
  --to <RECIPIENT_ADDRESS> \
  --amount 0.1 \
  --mode 3
```

Mode 1 (sign locally and relay) and Mode 3 (gateway transfer) are both supported. Intents are stored in `~/.solmesh/queue.json`.

**View queued intents:**
```bash
solmesh queue list
solmesh queue list --status pending --json
```

**Flush queued intents when a gateway is available:**
```bash
solmesh queue flush --auto-discover
solmesh queue flush --gateway-node '!aabbccdd' --wallet mywallet
```

**Manage the queue:**
```bash
solmesh queue clear --status failed
solmesh queue remove <INTENT_ID>
```

**Auto-flush daemon** -- listens for gateway beacons and flushes automatically:
```bash
solmesh listen --wallet mywallet --auto-discover
```

The daemon validates your passphrase upfront, caches it in memory, and flushes pending intents each time a gateway beacon is received.

### 9. HTTP API (Optional)

SolMesh can expose a REST API for programmatic access to gateway functions. Requires `pip install solmesh[http]`.

**Enable via CLI:**
```bash
solmesh gateway --http-port 8080 --api-key "your-secret-key" --rpc-url https://api.devnet.solana.com
```

**Or via config.yaml:**
```yaml
gateway:
  http_port: 8080
  api_key: "your-secret-key"
```

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| GET | /v1/status | Gateway info and uptime |
| GET | /v1/balance/{address}?token= | SOL or SPL token balance |
| GET | /v1/blockhash | Latest blockhash and last valid block height |
| GET | /v1/slot | Current Solana slot |
| POST | /v1/transfer | Submit transfer from gateway hot wallet |

All endpoints require an `X-API-Key` header. The OpenAPI 3.0 spec is available at `/openapi.json`.

**Examples:**
```bash
curl -H "X-API-Key: your-key" http://localhost:8080/v1/status
curl -H "X-API-Key: your-key" http://localhost:8080/v1/balance/<SOLANA_ADDRESS>
curl -H "X-API-Key: your-key" http://localhost:8080/openapi.json
curl -H "X-API-Key: your-key" -X POST http://localhost:8080/v1/transfer \
  -H "Content-Type: application/json" \
  -d '{"destination": "<SOLANA_ADDRESS>", "amount": 0.01}'
```

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit:

```bash
cp config.example.yaml config.yaml
```

Then run with:

```bash
solmesh -c config.yaml gateway
```

### Transfer Limits

SOL and token transfer limits are configured separately:

```yaml
max_transfer_sol: 0.1
max_transfer_usdc: 10.0
# Per-mint limits for other SPL tokens:
# token_limits:
#   "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 10.0
```

### Rate Limiting

The gateway rate-limits requests per sender using a token bucket algorithm:

```yaml
max_requests_per_minute: 10.0
rate_limit_burst: 3
```

### Gateway Beacon

The gateway periodically broadcasts a beacon advertising its capabilities and uptime:

```yaml
beacon_interval: 60  # seconds
```

### Transfer Authorization

Mode 3 requests are authorized by verifying the Ed25519 signature against the sender's Solana pubkey. The `allowed_requesters` list contains Solana pubkey strings (base58):

```yaml
allowed_requesters:
  - "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
  - "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
```

An empty list allows all authenticated requesters.

## Protocol

SolMesh uses a compact binary protocol designed for LoRa's ~237-byte message limit:

- **10-byte header**: magic (2B) + version (1B) + message type (1B) + message ID (2B) + chunk number (1B) + total chunks (1B) + payload length (1B) + CRC-8 (1B)
- **Up to 210 bytes payload per chunk**
- A typical SOL transfer (~215 bytes) fits in 2 chunks
- SPL token payloads include a flags byte and optional 32-byte mint address, backwards-compatible with SOL-only messages

Message types: `TX_CHUNK`, `TX_REQUEST`, `ADDR_SHARE`, `ACK`, `NACK`, `BALANCE_REQ`, `BALANCE_RESP`, `BLOCKHASH_REQ`, `BLOCKHASH_RESP`, `TX_RESULT`, `GATEWAY_BEACON`

## Security

- Private keys are **never** transmitted over LoRa
- Wallet files are encrypted with AES-256-GCM (PBKDF2-derived key, 480K iterations)
- Wallet files are created with `0600` permissions (owner read/write only)
- BIP39 mnemonic backup -- 24-word recovery phrase displayed once, never stored
- Mode 3 requests are authenticated via Ed25519 signatures verified against the sender's Solana pubkey (not the spoofable mesh node ID)
- Gateway enforces an allowlist of authorized Solana pubkeys and per-transfer limits (separate SOL and per-token limits)
- Per-sender token bucket rate limiting protects the gateway from abuse
- CRC-8 integrity check on all protocol messages (on top of LoRa's FEC)
- Chunk reassembly is keyed by `(sender_id, msg_id)` to prevent cross-sender collisions
- Store-and-forward queue stores only unsigned intent parameters (wallet name, recipient address, amount) -- **no secrets on disk**. Passphrases are held in memory only. Queue files use atomic writes (`tempfile` + `os.replace()`), `0600` permissions, and `fcntl.flock`-based file locking for inter-process safety. SENDING intents are recovered to PENDING on restart.
- HTTP API requires an API key (`X-API-Key` header) on every request. The gateway refuses to start the HTTP server without an `api_key` configured. HTTP callers get separate rate-limit buckets from mesh senders. Same transfer limits as mesh Mode 3.

## Disclaimer

SolMesh is experimental software provided "as-is" without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and noninfringement.

SolMesh is a self-custody tool -- keys are generated and stored locally on your device and are never sent to any server. You are solely responsible for your own wallets, private keys, mnemonic phrases, and funds.

The authors and contributors shall not be held liable for any loss of funds, lost keys, failed transactions, security breaches, hacks, or any other damages arising from the use of this software. By using SolMesh, you agree to hold the authors and contributors harmless from any and all claims, losses, or liabilities.

You are solely responsible for compliance with all applicable laws and regulations in your jurisdiction, including but not limited to sanctions, export controls, tax obligations, and financial regulations. The authors make no representations regarding the legality of using this software in any jurisdiction.

Use at your own risk. Always test on devnet before using real funds.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Requirements

- Python 3.9+
- Meshtastic device (USB or WiFi connected)
- `meshtastic`, `solders`, `solana`, `mnemonic` Python libraries

## License

MIT -- [Heathen](https://x.com/heathenft)
