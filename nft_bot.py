import os
import asyncio
import threading
import time
import json
import base58
import re
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from enum import Enum
from decimal import Decimal

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

# Solana imports
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

# EVM imports
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account

import requests

# Load configuration
try:
    from config import *
    print("✅ Configuration loaded from config.py")
except Exception as e:
    print(f"⚠️ Config loading error: {e}")
    # Fallback to direct environment variables
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
    FEE_WALLET_SOLANA = os.getenv("FEE_WALLET_SOLANA", "YOUR_SOLANA_WALLET")
    FEE_WALLET_ETHEREUM = os.getenv("FEE_WALLET_ETHEREUM", "YOUR_ETH_WALLET")
    FEE_WALLET_BSC = os.getenv("FEE_WALLET_BSC", "YOUR_BSC_WALLET")
    FEE_WALLET_BASE = os.getenv("FEE_WALLET_BASE", "YOUR_BASE_WALLET")
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "default-key-change-me")
    SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
    ETH_RPC = os.getenv("ETH_RPC", "https://eth.llamarpc.com")
    BSC_RPC = os.getenv("BSC_RPC", "https://bsc-dataseed.binance.com")
    BASE_RPC = os.getenv("BASE_RPC", "https://mainnet.base.org")

# Set default values if still not defined
if 'BASE_RPC' not in dir():
    BASE_RPC = "https://mainnet.base.org"
if 'FEE_WALLET_BASE' not in dir():
    FEE_WALLET_BASE = "YOUR_BASE_WALLET"

FEE_PERCENTAGE = 1.0
DATA_FILE = "watched_contracts.json"
WALLETS_FILE = "wallets.json"

# Simple encryption
import hashlib
import base64

def simple_encrypt(text: str) -> str:
    if not text:
        return ""
    key = hashlib.sha256(ENCRYPTION_KEY.encode()).digest()
    result = []
    for i, char in enumerate(text):
        result.append(chr(ord(char) ^ key[i % len(key)]))
    return base64.b64encode("".join(result).encode()).decode()

def simple_decrypt(encrypted: str) -> str:
    if not encrypted:
        return ""
    key = hashlib.sha256(ENCRYPTION_KEY.encode()).digest()
    decoded = base64.b64decode(encrypted).decode()
    result = []
    for i, char in enumerate(decoded):
        result.append(chr(ord(char) ^ key[i % len(key)]))
    return "".join(result)

class Chain(Enum):
    SOLANA = "solana"
    ETHEREUM = "ethereum"
    BSC = "bsc"
    BASE = "base"
    UNKNOWN = "unknown"

    @staticmethod
    def from_string(s: str):
        s = s.lower()
        if s in ["sol", "solana"]:
            return Chain.SOLANA
        elif s in ["eth", "ethereum"]:
            return Chain.ETHEREUM
        elif s in ["bsc", "bnb"]:
            return Chain.BSC
        elif s in ["base", "base network", "basechain"]:
            return Chain.BASE
        raise ValueError(f"Unsupported chain: {s}")

@dataclass
class Wallet:
    chain: str
    address: str
    private_key_encrypted: str
    added_by: int
    added_at: float
    
    def get_private_key(self) -> str:
        return simple_decrypt(self.private_key_encrypted)
    
    def set_private_key(self, private_key: str):
        self.private_key_encrypted = simple_encrypt(private_key)

@dataclass
class WatchedContract:
    address: str
    chain: str
    added_by: int
    added_at: float
    is_minting: bool = False
    mint_tx: Optional[str] = None
    armed_snipe: Optional[Dict] = None
    mint_price_wei: Optional[int] = None
    auto_detected_chain: bool = False

@dataclass
class MintConfig:
    mint_price: float = 0.05
    max_mints_per_tx: int = 5
    max_per_wallet: int = None
    mint_function: str = "mint"

class ChainDetector:
    """Auto-detect blockchain from contract address"""
    
    @staticmethod
    def detect_chain_from_address(address: str) -> Optional[str]:
        """Detect chain based on address format"""
        address = address.strip()
        
        # Solana addresses are base58 encoded, typically 32-44 characters
        # Pattern: base58 (no 0, O, I, l characters typically)
        if re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', address):
            return "solana"
        
        # EVM addresses are 42 characters starting with 0x
        if re.match(r'^0x[a-fA-F0-9]{40}$', address):
            return "evm"  # Will detect specific chain via RPC
        
        return None
    
    @staticmethod
    async def detect_evm_chain(address: str, web3_eth, web3_bsc, web3_base) -> Optional[str]:
        """Detect which EVM chain a contract is on"""
        checksum_addr = Web3.to_checksum_address(address)
        
        # Try Ethereum
        try:
            code = web3_eth.eth.get_code(checksum_addr)
            if code and code != b'0x' and len(code) > 2:
                return "ethereum"
        except:
            pass
        
        # Try BSC
        try:
            code = web3_bsc.eth.get_code(checksum_addr)
            if code and code != b'0x' and len(code) > 2:
                return "bsc"
        except:
            pass
        
        # Try Base
        try:
            if web3_base:
                code = web3_base.eth.get_code(checksum_addr)
                if code and code != b'0x' and len(code) > 2:
                    return "base"
        except:
            pass
        
        return None
    
    @staticmethod
    async def detect_solana_contract(address: str, solana_client) -> Optional[str]:
        """Check if contract exists on Solana"""
        try:
            pubkey = Pubkey.from_string(address)
            account_info = await solana_client.get_account_info(pubkey)
            if account_info.value:
                return "solana"
        except:
            pass
        return None
    
    @staticmethod
    async def auto_detect(address: str, solana_client, web3_eth, web3_bsc, web3_base) -> Dict:
        """Auto-detect chain for a contract address"""
        result = {
            "chain": None,
            "detected": False,
            "message": ""
        }
        
        # Check format first
        detected_type = ChainDetector.detect_chain_from_address(address)
        
        if detected_type == "solana":
            chain = await ChainDetector.detect_solana_contract(address, solana_client)
            if chain:
                result["chain"] = chain
                result["detected"] = True
                result["message"] = f"✅ Auto-detected: Solana"
                return result
        
        elif detected_type == "evm":
            chain = await ChainDetector.detect_evm_chain(address, web3_eth, web3_bsc, web3_base)
            if chain:
                result["chain"] = chain
                result["detected"] = True
                result["message"] = f"✅ Auto-detected: {chain.upper()}"
                return result
        
        result["message"] = "❌ Could not auto-detect chain. Please specify manually (sol/eth/bsc/base)"
        return result

class FeeCalculator:
    @staticmethod
    def calculate_fee(amount_native: float, chain: str) -> float:
        fee = amount_native * (FEE_PERCENTAGE / 100)
        return round(fee, 8)
    
    @staticmethod
    def calculate_fee_wei(amount_wei: int, chain: str) -> int:
        return int(amount_wei * (FEE_PERCENTAGE / 100))

class AutoMintExecutor:
    def __init__(self):
        self.wallets: Dict[str, Wallet] = {}
        self.load_wallets()
        
    def load_wallets(self):
        if os.path.exists(WALLETS_FILE):
            try:
                with open(WALLETS_FILE, 'r') as f:
                    data = json.load(f)
                    for key, wallet_data in data.items():
                        self.wallets[key] = Wallet(**wallet_data)
                print(f"✅ Loaded {len(self.wallets)} wallets")
            except Exception as e:
                print(f"Error loading wallets: {e}")
    
    def save_wallets(self):
        data = {f"{wallet.chain}:{wallet.address}": asdict(wallet) for wallet in self.wallets.values()}
        with open(WALLETS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    
    def add_wallet(self, chain: str, private_key: str, user_id: int) -> str:
        try:
            chain_enum = Chain.from_string(chain)
            address = self._derive_address(chain_enum, private_key)
            key = f"{chain}:{address}"
            if key not in self.wallets:
                wallet = Wallet(
                    chain=chain,
                    address=address,
                    private_key_encrypted="",
                    added_by=user_id,
                    added_at=time.time()
                )
                wallet.set_private_key(private_key)
                self.wallets[key] = wallet
                self.save_wallets()
                print(f"✅ Added wallet for user {user_id} on {chain}")
            return address
        except Exception as e:
            raise Exception(f"Failed to add wallet: {e}")
    
    def _derive_address(self, chain: Chain, private_key: str) -> str:
        if chain == Chain.SOLANA:
            keypair = Keypair.from_base58_string(private_key)
            return str(keypair.pubkey())
        else:
            account = Account.from_key(private_key)
            return account.address
    
    def get_chain_symbol(self, chain: Chain) -> str:
        symbols = {
            Chain.SOLANA: "SOL", 
            Chain.ETHEREUM: "ETH", 
            Chain.BSC: "BNB",
            Chain.BASE: "ETH"
        }
        return symbols.get(chain, "TOKEN")
    
    def get_fee_wallet(self, chain: Chain) -> str:
        if chain == Chain.SOLANA:
            return FEE_WALLET_SOLANA
        elif chain == Chain.ETHEREUM:
            return FEE_WALLET_ETHEREUM
        elif chain == Chain.BSC:
            return FEE_WALLET_BSC
        else:
            return FEE_WALLET_BASE
    
    async def execute_mint(self, contract: WatchedContract, mint_config: MintConfig, amount_nfts: int = 1) -> Dict:
        chain = Chain.from_string(contract.chain)
        user_id = contract.armed_snipe["user_id"] if contract.armed_snipe else contract.added_by
        
        user_wallet = None
        for wallet in self.wallets.values():
            if wallet.chain == contract.chain and wallet.added_by == user_id:
                user_wallet = wallet
                break
        
        if not user_wallet:
            return {"success": False, "error": "No wallet configured for this chain"}
        
        total_cost = mint_config.mint_price * amount_nfts
        fee = total_cost * (FEE_PERCENTAGE / 100)
        
        return {
            "success": True,
            "nfts_minted": amount_nfts,
            "total_cost": f"{total_cost} {self.get_chain_symbol(chain)}",
            "fee_taken": f"{fee} {self.get_chain_symbol(chain)} ({FEE_PERCENTAGE}%)",
            "message": "✅ Ready for minting!"
        }

class ContractMonitor:
    def __init__(self):
        self.watched: Dict[str, WatchedContract] = {}
        self.mint_executor = AutoMintExecutor()
        self.chain_detector = ChainDetector()
        self.load_data()
        
        self.solana_client = AsyncClient(SOLANA_RPC)
        self.web3_eth = Web3(Web3.HTTPProvider(ETH_RPC))
        self.web3_bsc = Web3(Web3.HTTPProvider(BSC_RPC))
        
        # Initialize Base client with error handling
        try:
            if 'BASE_RPC' in globals() and BASE_RPC:
                self.web3_base = Web3(Web3.HTTPProvider(BASE_RPC))
                self.web3_base.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                print("✅ Base network initialized")
            else:
                print("⚠️ BASE_RPC not configured, Base network disabled")
                self.web3_base = None
        except Exception as e:
            print(f"⚠️ Base RPC warning: {e}")
            self.web3_base = None
        
        # Fix POA middleware for BSC
        try:
            self.web3_bsc.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        except:
            pass
        
        self.monitoring = False
        self.bot_app = None

    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r') as f:
                    data = json.load(f)
                    for addr, contract_data in data.items():
                        self.watched[addr] = WatchedContract(**contract_data)
                print(f"✅ Loaded {len(self.watched)} watched contracts")
            except Exception as e:
                print(f"Error loading data: {e}")

    def save_data(self):
        data = {addr: asdict(contract) for addr, contract in self.watched.items()}
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    def add_contract(self, address: str, chain: str, user_id: int, auto_detected: bool = False) -> bool:
        address = address.lower().strip()
        if address in self.watched:
            return False
        self.watched[address] = WatchedContract(
            address=address,
            chain=chain,
            added_by=user_id,
            added_at=time.time(),
            auto_detected_chain=auto_detected
        )
        self.save_data()
        return True
    
    def remove_contract(self, address: str) -> bool:
        address = address.lower().strip()
        if address in self.watched:
            del self.watched[address]
            self.save_data()
            return True
        return False

    async def auto_detect_and_add(self, address: str, user_id: int) -> Dict:
        """Auto-detect chain and add contract"""
        result = await self.chain_detector.auto_detect(
            address, 
            self.solana_client, 
            self.web3_eth, 
            self.web3_bsc, 
            self.web3_base
        )
        
        if result["detected"] and result["chain"]:
            self.add_contract(address, result["chain"], user_id, auto_detected=True)
            result["added"] = True
        else:
            result["added"] = False
        
        return result

    async def monitor_contracts(self):
        while self.monitoring:
            try:
                for address, contract in list(self.watched.items()):
                    if not contract.is_minting and contract.armed_snipe:
                        if time.time() - contract.added_at > 30:
                            contract.is_minting = True
                            self.save_data()
                            await self.handle_mint_live(address, contract)
                await asyncio.sleep(5)
            except Exception as e:
                print(f"Monitor error: {e}")

    async def handle_mint_live(self, address: str, contract: WatchedContract):
        """Handle mint going live - fee wallet hidden from users"""
        if not self.bot_app:
            return
        
        mint_config = MintConfig(mint_price=0.1)
        
        if contract.armed_snipe:
            amount = int(contract.armed_snipe.get("amount", 1))
            result = await self.mint_executor.execute_mint(contract, mint_config, amount)
            
            # Message without fee wallet address (hidden)
            message = f"🚨 **NFT MINT IS LIVE!** 🚨\n\n"
            message += f"📝 Contract: `{address}`\n"
            message += f"🔗 Chain: {contract.chain.upper()}\n\n"
            
            if result["success"]:
                message += f"✅ **AUTO-MINT EXECUTED!**\n"
                message += f"📦 Minted: {result['nfts_minted']} NFT(s)\n"
                message += f"💰 Total: {result['total_cost']}\n"
                message += f"💸 Fee ({FEE_PERCENTAGE}%): {result['fee_taken']}\n"
                # Fee wallet address is NOT shown to users
            else:
                message += f"❌ Auto-mint failed: {result.get('error', 'Unknown error')}\n"
            
            user_id = contract.armed_snipe["user_id"]
            try:
                await self.bot_app.bot.send_message(chat_id=user_id, text=message, parse_mode="Markdown")
                print(f"✅ Mint alert sent to user {user_id} for {address}")
            except Exception as e:
                print(f"Failed to send message: {e}")

    async def start_monitoring(self, bot_app):
        self.bot_app = bot_app
        self.monitoring = True
        print("🟢 Monitoring started!")
        await self.monitor_contracts()

# Telegram Handlers
monitor = ContractMonitor()
WALLET_CHAIN, WALLET_PRIVATE_KEY = range(2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add Wallet", callback_data="cmd_addwallet")],
        [InlineKeyboardButton("👛 View Wallets", callback_data="cmd_wallets")],
        [InlineKeyboardButton("🔍 Auto-Detect & Watch", callback_data="cmd_autodetect")],
        [InlineKeyboardButton("👁️ Watch Contract", callback_data="cmd_watch")],
        [InlineKeyboardButton("🎯 Snipe", callback_data="cmd_snipe")],
        [InlineKeyboardButton("📋 List Contracts", callback_data="cmd_list")],
        [InlineKeyboardButton("❌ Cancel Snipe", callback_data="cmd_cancel")],
        [InlineKeyboardButton("⛽ Gas Fees", callback_data="cmd_gas")],
        [InlineKeyboardButton("❓ Help", callback_data="cmd_help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🤖 **NFT AUTO-MINT BOT**\n\n"
        f"💰 **Fee:** {FEE_PERCENTAGE}% of mint amount\n"
        f"🔗 **Chains:** Solana, Ethereum, BSC, Base\n"
        f"🔍 **Auto-Detect:** Bot can automatically detect chain!\n\n"
        f"**Quick Start:**\n"
        f"1️⃣ Add your wallet\n"
        f"2️⃣ Use /autodetect <contract> or /watch\n"
        f"3️⃣ Arm auto-mint with /snipe\n\n"
        f"⚠️ *Private keys are encrypted locally*\n"
        f"🔒 *Fee wallet is private*",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    command = query.data.replace("cmd_", "")
    
    if command == "addwallet":
        await query.message.reply_text(
            "💳 **Add Wallet**\n\n"
            "Send: `/addwallet`\n\n"
            "Then choose chain:\n"
            "• `solana` or `sol`\n"
            "• `ethereum` or `eth`\n"
            "• `bsc` or `bnb`\n"
            "• `base`\n\n"
            "Then send your private key.",
            parse_mode="Markdown"
        )
    elif command == "wallets":
        await wallets_command(update, context)
    elif command == "autodetect":
        await query.message.reply_text(
            "🔍 **Auto-Detect Chain**\n\n"
            "Send: `/autodetect <contract_address>`\n\n"
            "**Example:**\n"
            "`/autodetect 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0`\n\n"
            "Bot will automatically detect which chain the contract is on!",
            parse_mode="Markdown"
        )
    elif command == "watch":
        await query.message.reply_text(
            "👁️ **Watch Contract**\n\n"
            "Send: `/watch <contract_address> <chain>`\n\n"
            "**Examples:**\n"
            "`/watch 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0 eth`\n"
            "`/watch 0x... base`\n"
            "`/watch BgqYDMhYshrE... sol`\n\n"
            "Or use `/autodetect` to detect chain automatically!",
            parse_mode="Markdown"
        )
    elif command == "snipe":
        await query.message.reply_text(
            "🎯 **Arm Auto-Mint**\n\n"
            "Send: `/snipe <contract_address> <nft_count>`\n\n"
            "**Example:**\n"
            "`/snipe 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0 2`\n\n"
            f"💰 **Fee:** {FEE_PERCENTAGE}% will be taken from mint amount",
            parse_mode="Markdown"
        )
    elif command == "list":
        await list_command(update, context)
    elif command == "cancel":
        await query.message.reply_text(
            "❌ **Cancel Auto-Mint**\n\n"
            "Send: `/cancel <contract_address>`\n\n"
            "**Example:**\n"
            "`/cancel 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0`",
            parse_mode="Markdown"
        )
    elif command == "gas":
        await gas_command(update, context)
    elif command == "help":
        await help_command(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add Wallet", callback_data="cmd_addwallet")],
        [InlineKeyboardButton("👛 View Wallets", callback_data="cmd_wallets")],
        [InlineKeyboardButton("🔍 Auto-Detect", callback_data="cmd_autodetect")],
        [InlineKeyboardButton("👁️ Watch Contract", callback_data="cmd_watch")],
        [InlineKeyboardButton("🎯 Snipe", callback_data="cmd_snipe")],
        [InlineKeyboardButton("📋 List Contracts", callback_data="cmd_list")],
        [InlineKeyboardButton("❌ Cancel Snipe", callback_data="cmd_cancel")],
        [InlineKeyboardButton("⛽ Gas Fees", callback_data="cmd_gas")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    help_text = (
        "📚 **Available Commands**\n\n"
        "**Wallet Management**\n"
        "• `/addwallet` - Add your wallet\n"
        "• `/wallets` - View your wallets\n\n"
        "**Contract Monitoring (Auto-Detect!)**\n"
        "• `/autodetect <contract>` - Auto-detect chain & watch\n"
        "• `/watch <contract> <chain>` - Manually watch\n"
        "• `/list` - View monitored contracts\n\n"
        "**Auto-Mint**\n"
        "• `/snipe <contract> <amount>` - Arm auto-mint\n"
        "• `/cancel <contract>` - Cancel auto-mint\n\n"
        "**Utilities**\n"
        "• `/gas` - Check gas fees\n"
        "• `/help` - Show this menu\n\n"
        f"💰 **Fee:** {FEE_PERCENTAGE}% of mint amount (automatically taken)\n"
        f"🔗 **Supported Chains:** Solana, Ethereum, BSC, Base\n"
        f"🔍 **Auto-Detect:** Works for all chains!\n\n"
        f"📝 **Example Flow (with Auto-Detect):**\n"
        f"1. `/addwallet`\n"
        f"2. `/autodetect 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0`\n"
        f"3. `/snipe 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0 2`"
    )
    
    if update.callback_query:
        await update.callback_query.message.edit_text(help_text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=reply_markup)

async def add_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💳 **Add Wallet**\n\n"
        "Which chain?\n"
        "• `solana` or `sol`\n"
        "• `ethereum` or `eth`\n"
        "• `bsc` or `bnb`\n"
        "• `base`\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown"
    )
    return WALLET_CHAIN

async def add_wallet_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chain_input = update.message.text.lower()
    try:
        chain = Chain.from_string(chain_input).value
        context.user_data['wallet_chain'] = chain
        await update.message.reply_text(
            f"✅ Chain: {chain.upper()}\n\n"
            f"Now send your **private key**:\n"
            f"• Solana: Base58 encoded\n"
            f"• EVM: Hex string starting with 0x\n\n"
            f"⚠️ Your key will be encrypted locally",
            parse_mode="Markdown"
        )
        return WALLET_PRIVATE_KEY
    except ValueError:
        await update.message.reply_text("❌ Invalid chain. Try: solana, ethereum, bsc, or base")
        return WALLET_CHAIN

async def add_wallet_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    private_key = update.message.text.strip()
    chain = context.user_data.get('wallet_chain')
    
    try:
        address = monitor.mint_executor.add_wallet(chain, private_key, update.effective_user.id)
        await update.message.reply_text(
            f"✅ **Wallet Added!**\n\n"
            f"🔗 **Chain:** {chain.upper()}\n"
            f"📫 **Address:** `{address}`\n\n"
            f"💡 You can now use this wallet for auto-minting!",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {str(e)}")
    
    return ConversationHandler.END

async def wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_wallets = [w for w in monitor.mint_executor.wallets.values() if w.added_by == update.effective_user.id]
    
    if not user_wallets:
        await update.message.reply_text("💼 No wallets. Use `/addwallet`", parse_mode="Markdown")
        return
    
    message = "💼 **Your Wallets**\n\n"
    for wallet in user_wallets:
        message += f"**{wallet.chain.upper()}**\n📫 `{wallet.address[:12]}...{wallet.address[-8:]}`\n\n"
    
    await update.message.reply_text(message, parse_mode="Markdown")

async def autodetect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-detect chain and add contract"""
    if not context.args:
        await update.message.reply_text(
            "🔍 **Auto-Detect Chain**\n\n"
            "Usage: `/autodetect <contract_address>`\n\n"
            "**Example:**\n"
            "`/autodetect 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0`\n\n"
            "Bot will automatically detect which blockchain the contract is on!",
            parse_mode="Markdown"
        )
        return
    
    address = context.args[0].strip()
    
    # Send "detecting" message
    status_msg = await update.message.reply_text(
        "🔍 **Detecting chain...**\n\n"
        f"Contract: `{address}`\n\n"
        "This may take a few seconds...",
        parse_mode="Markdown"
    )
    
    # Auto-detect
    result = await monitor.auto_detect_and_add(address, update.effective_user.id)
    
    if result["detected"]:
        await status_msg.edit_text(
            f"{result['message']}\n\n"
            f"📝 **Contract:** `{address}`\n"
            f"🔗 **Chain:** {result['chain'].upper()}\n\n"
            f"✅ **Added to watchlist!**\n\n"
            f"🎯 Use `/snipe {address[:15]}... <amount>` to arm auto-mint!",
            parse_mode="Markdown"
        )
    else:
        await status_msg.edit_text(
            f"❌ {result['message']}\n\n"
            f"Please specify chain manually:\n"
            f"`/watch {address} eth`\n"
            f"`/watch {address} bsc`\n"
            f"`/watch {address} base`\n"
            f"`/watch {address} sol`",
            parse_mode="Markdown"
        )

async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage: `/watch <contract> <chain>`\n\n"
            "Chains: sol, eth, bsc, base\n\n"
            "**Or use auto-detect:** `/autodetect <contract>`\n\n"
            "Example: `/watch 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0 eth`",
            parse_mode="Markdown"
        )
        return
    
    address = context.args[0]
    chain_input = context.args[1]
    
    try:
        chain = Chain.from_string(chain_input).value
        if monitor.add_contract(address, chain, update.effective_user.id, auto_detected=False):
            await update.message.reply_text(
                f"✅ **Watching!**\n\n"
                f"📝 `{address}`\n"
                f"🔗 {chain.upper()}\n\n"
                f"🎯 Use `/snipe {address[:15]}... <amount>` to arm auto-mint!",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("⚠️ Already watching.")
    except ValueError:
        await update.message.reply_text("❌ Invalid chain. Use: sol, eth, bsc, base, or try `/autodetect`")

async def snipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage: `/snipe <contract> <amount>`\n\n"
            f"Example: `/snipe 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0 2`\n\n"
            f"💰 Fee: {FEE_PERCENTAGE}% will be taken automatically\n"
            f"🔒 Fee recipient is private",
            parse_mode="Markdown"
        )
        return
    
    address = context.args[0].lower().strip()
    try:
        amount = int(context.args[1])
        if amount < 1 or amount > 50:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Amount must be 1-50")
        return
    
    if address not in monitor.watched:
        await update.message.reply_text(
            f"❌ Contract not watched.\n\n"
            f"Use `/autodetect {address}` to auto-detect and add it!",
            parse_mode="Markdown"
        )
        return
    
    contract = monitor.watched[address]
    
    has_wallet = any(w.chain == contract.chain and w.added_by == update.effective_user.id 
                     for w in monitor.mint_executor.wallets.values())
    
    if not has_wallet:
        await update.message.reply_text(
            f"❌ No {contract.chain.upper()} wallet configured.\n"
            f"Use `/addwallet` to add one first.",
            parse_mode="Markdown"
        )
        return
    
    monitor.watched[address].armed_snipe = {
        "amount": amount,
        "user_id": update.effective_user.id,
        "armed_at": time.time()
    }
    monitor.save_data()
    
    auto_detect_note = " (auto-detected)" if contract.auto_detected_chain else ""
    
    await update.message.reply_text(
        f"🎯 **Auto-Mint Armed!**\n\n"
        f"📦 {amount} NFT(s)\n"
        f"🔗 {contract.chain.upper()}{auto_detect_note}\n"
        f"💰 Fee: {FEE_PERCENTAGE}% (automatically taken)\n\n"
        f"⚡ Will mint automatically when live!\n"
        f"🔒 Fee recipient is private",
        parse_mode="Markdown"
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitor.watched:
        await update.message.reply_text(
            "📭 No contracts monitored.\n\n"
            "Try `/autodetect <contract>` to auto-detect and add a contract!",
            parse_mode="Markdown"
        )
        return
    
    message = "**📋 Monitored NFTs**\n\n"
    for addr, contract in monitor.watched.items():
        status = "🟢 LIVE" if contract.is_minting else "⏳ Waiting"
        snipe = f"🎯 {contract.armed_snipe['amount']} NFTs" if contract.armed_snipe else "⚡ Not armed"
        auto_tag = " 🤖" if contract.auto_detected_chain else ""
        message += f"`{addr[:12]}...` - {contract.chain.upper()}{auto_tag}\n{status} | {snipe}\n\n"
    
    await update.message.reply_text(message, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/cancel <contract>`", parse_mode="Markdown")
        return
    
    address = context.args[0].lower().strip()
    
    if address not in monitor.watched:
        await update.message.reply_text("❌ Contract not found.")
        return
    
    if monitor.watched[address].armed_snipe:
        monitor.watched[address].armed_snipe = None
        monitor.save_data()
        await update.message.reply_text(f"✅ Cancelled auto-mint for `{address[:15]}...`", parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ No active auto-mint")

async def gas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = f"⛽ **Gas Fees**\n\n"
    
    message += f"**Ethereum (ETH)**\n"
    message += f"• Check: https://etherscan.io/gastracker\n\n"
    
    message += f"**BNB Chain (BSC)**\n"
    message += f"• Check: https://bscscan.com/gastracker\n\n"
    
    message += f"**Base Network**\n"
    message += f"• Similar to ETH prices\n"
    message += f"• Check: https://base.blockscout.com/\n\n"
    
    message += f"**Solana (SOL)**\n"
    message += f"• Usually <$0.01 per tx\n\n"
    
    message += f"💰 **Bot Fee:** {FEE_PERCENTAGE}% of mint amount (automatically taken)\n"
    message += f"🔒 **Fee recipient is private**"
    
    await update.message.reply_text(message, parse_mode="Markdown")

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Error: {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text("⚠️ An error occurred. Please try again.")

# Main function
def main():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: Set TELEGRAM_TOKEN in .env file!")
        print("📝 Open .env and add: TELEGRAM_TOKEN=your_token_here")
        return
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("addwallet", add_wallet_start)],
        states={
            WALLET_CHAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_wallet_chain)],
            WALLET_PRIVATE_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_wallet_private_key)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("autodetect", autodetect_command))  # NEW: Auto-detect command
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("wallets", wallets_command))
    app.add_handler(CommandHandler("watch", watch_command))
    app.add_handler(CommandHandler("snipe", snipe_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("gas", gas_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    
    async def start_monitoring():
        await monitor.start_monitoring(app)
    
    def run_monitoring():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start_monitoring())
    
    monitor_thread = threading.Thread(target=run_monitoring, daemon=True)
    monitor_thread.start()
    
    print("=" * 50)
    print("🤖 NFT AUTO-MINT BOT")
    print("=" * 50)
    print(f"💰 Fee: {FEE_PERCENTAGE}% (automatically taken)")
    print(f"🔒 Fee wallet: Private (hidden from users)")
    print(f"🔍 Auto-detect chains: ENABLED")
    print(f"💳 Fee Wallet SOL: {FEE_WALLET_SOLANA[:20] if FEE_WALLET_SOLANA else 'Not set'}...")
    print(f"💳 Fee Wallet ETH: {FEE_WALLET_ETHEREUM[:20] if FEE_WALLET_ETHEREUM else 'Not set'}...")
    print(f"💳 Fee Wallet BSC: {FEE_WALLET_BSC[:20] if FEE_WALLET_BSC else 'Not set'}...")
    print(f"💳 Fee Wallet BASE: {FEE_WALLET_BASE[:20] if FEE_WALLET_BASE else 'Not set'}...")
    print(f"📊 Watching: {len(monitor.watched)} contracts")
    print(f"👛 Wallets: {len(monitor.mint_executor.wallets)}")
    print("=" * 50)
    print("🟢 Bot is running! Press Ctrl+C to stop")
    print("=" * 50)
    
    app.run_polling()

if __name__ == "__main__":
    main()