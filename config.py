import os
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# Bot Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Fee Wallets
FEE_WALLET_SOLANA = os.getenv("FEE_WALLET_SOLANA")
FEE_WALLET_ETHEREUM = os.getenv("FEE_WALLET_ETHEREUM")
FEE_WALLET_BSC = os.getenv("FEE_WALLET_BSC")
FEE_WALLET_BASE = os.getenv("FEE_WALLET_BASE")

# Encryption
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "default-encryption-key-change-this")

# RPC Endpoints
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
ETH_RPC = os.getenv("ETH_RPC", "https://eth.llamarpc.com")
BSC_RPC = os.getenv("BSC_RPC", "https://bsc-dataseed.binance.com")
BASE_RPC = os.getenv("BASE_RPC", "https://mainnet.base.org")

# Print loaded config (for debugging)
print(f"✅ Configuration loaded!")
print(f"📱 Bot Token: {TELEGRAM_TOKEN[:15] if TELEGRAM_TOKEN else 'NOT SET'}...")
print(f"💰 SOL Fee Wallet: {FEE_WALLET_SOLANA[:20] if FEE_WALLET_SOLANA else 'NOT SET'}...")
print(f"💰 ETH Fee Wallet: {FEE_WALLET_ETHEREUM[:20] if FEE_WALLET_ETHEREUM else 'NOT SET'}...")
print(f"💰 BSC Fee Wallet: {FEE_WALLET_BSC[:20] if FEE_WALLET_BSC else 'NOT SET'}...")
print(f"💰 BASE Fee Wallet: {FEE_WALLET_BASE[:20] if FEE_WALLET_BASE else 'NOT SET'}...")
print(f"🔗 Base RPC: {BASE_RPC}")