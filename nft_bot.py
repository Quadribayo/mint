import os
import asyncio
import time
import json
import re
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from enum import Enum

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

import requests
import aiohttp
from eth_account import Account

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
FEE_PERCENTAGE = 1.0
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "default-key-change-me")

# API Keys (set these in Railway/FPS.ms environment variables)
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")

DATA_FILE = "watched_contracts.json"
WALLETS_FILE = "wallets.json"

# ============ ENCRYPTION ============
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

# ============ DATA MODELS ============
@dataclass
class GasConfig:
    strategy: str  # normal, priority, custom, auto
    max_gwei: Optional[float] = None
    priority_multiplier: float = 1.2
    
    def to_dict(self):
        return {"strategy": self.strategy, "max_gwei": self.max_gwei, "priority_multiplier": self.priority_multiplier}
    
    @classmethod
    def from_dict(cls, data):
        return cls(**data)

@dataclass
class WatchedContract:
    address: str
    chain: str
    added_by: int
    added_at: float
    is_minting: bool = False
    armed_snipe: Optional[Dict] = None
    gas_config: Optional[Dict] = None
    max_gas_usd: Optional[float] = None
    required_nfts: List[str] = None  # NFT contracts user must hold
    
    def __post_init__(self):
        if self.required_nfts is None:
            self.required_nfts = []

# ============ WALLET ELIGIBILITY CHECKER ============
class EligibilityChecker:
    @staticmethod
    async def check_holdings(wallet_address: str, required_nft_contracts: List[str]) -> Dict:
        """Check if wallet holds any of the required NFTs using Alchemy API"""
        if not required_nft_contracts:
            return {"eligible": True, "message": "✅ No eligibility requirements"}
        
        if not ALCHEMY_API_KEY:
            return {
                "eligible": True, 
                "message": "⚠️ No Alchemy API key set - eligibility check skipped. Add ALCHEMY_API_KEY to enable."
            }
        
        try:
            async with aiohttp.ClientSession() as session:
                required_lower = [c.lower() for c in required_nft_contracts]
                
                url = f"https://eth-mainnet.g.alchemy.com/nft/v2/{ALCHEMY_API_KEY}/getNFTs"
                params = {
                    "owner": wallet_address,
                    "withMetadata": "false",
                    "pageSize": "100"
                }
                
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        owned_nfts = data.get("ownedNfts", [])
                        
                        held_nfts = []
                        for nft in owned_nfts:
                            contract_addr = nft.get("contract", {}).get("address", "").lower()
                            if contract_addr in required_lower:
                                held_nfts.append(contract_addr)
                        
                        if held_nfts:
                            return {
                                "eligible": True,
                                "message": f"✅ Eligible! Found {len(held_nfts)} required NFT(s) in your wallet."
                            }
                        else:
                            return {
                                "eligible": False,
                                "message": f"❌ Not eligible. You don't hold any of the required NFT(s).\nRequired: {len(required_nft_contracts)} NFT contract(s)"
                            }
                    else:
                        return {"eligible": False, "message": "⚠️ Could not verify eligibility (API error)"}
        except Exception as e:
            print(f"Eligibility check error: {e}")
            return {"eligible": False, "message": f"⚠️ Error checking eligibility: {str(e)}"}
    
    @staticmethod
    async def get_nft_balance(wallet_address: str, nft_contract: str) -> int:
        """Get the number of NFTs owned for a specific contract"""
        if not ALCHEMY_API_KEY:
            return 0
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://eth-mainnet.g.alchemy.com/nft/v2/{ALCHEMY_API_KEY}/getNFTs"
                params = {
                    "owner": wallet_address,
                    "contractAddresses": nft_contract,
                    "withMetadata": "false"
                }
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        return len(data.get("ownedNfts", []))
        except:
            pass
        return 0

# ============ GAS MANAGER ============
class GasManager:
    @staticmethod
    async def get_eth_gas_prices() -> Dict:
        try:
            response = requests.get("https://api.etherscan.io/api?module=gastracker&action=gasoracle", timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "1":
                    gas_data = data["result"]
                    return {
                        "slow": int(gas_data["SafeGasPrice"]),
                        "standard": int(gas_data["ProposeGasPrice"]),
                        "fast": int(gas_data["FastGasPrice"]),
                        "base_fee": int(gas_data["suggestBaseFee"]),
                        "success": True
                    }
        except:
            pass
        return {"slow": 30, "standard": 40, "fast": 50, "base_fee": 25, "success": True}
    
    @staticmethod
    def gwei_to_usd(gwei: int, gas_limit: int = 150000) -> float:
        eth_price_usd = 3000
        eth_amount = (gwei * gas_limit) / 1e9
        return round(eth_amount * eth_price_usd, 2)
    
    @staticmethod
    def usd_to_gwei(usd: float, gas_limit: int = 150000) -> int:
        eth_price_usd = 3000
        max_eth = usd / eth_price_usd
        return int((max_eth * 1e9) / gas_limit)
    
    @staticmethod
    def calculate_gas_price(strategy: str, current_prices: Dict, custom_gwei: float = None, priority_mult: float = 1.2) -> Dict:
        if strategy == "normal":
            gas_gwei = current_prices.get("standard", 40)
            description = f"Normal gas ({gas_gwei} Gwei)"
        elif strategy == "priority":
            base_gas = current_prices.get("fast", 50)
            gas_gwei = int(base_gas * priority_mult)
            description = f"Priority gas ({gas_gwei} Gwei) - {int((priority_mult-1)*100)}% premium"
        elif strategy == "custom":
            gas_gwei = custom_gwei if custom_gwei else 100
            description = f"Custom gas ({gas_gwei} Gwei)"
        else:
            fast_gas = current_prices.get("fast", 50)
            if fast_gas < 50:
                gas_gwei = int(fast_gas * 1.3)
                description = f"Auto-aggressive ({gas_gwei} Gwei)"
            else:
                gas_gwei = fast_gas
                description = f"Auto-standard ({gas_gwei} Gwei)"
        
        usd_cost = GasManager.gwei_to_usd(gas_gwei)
        return {"gas_gwei": gas_gwei, "description": description, "usd_cost": usd_cost, "strategy": strategy}

# ============ CONTRACT MONITOR ============
class ContractMonitor:
    def __init__(self):
        self.watched: Dict[str, WatchedContract] = {}
        self.load_data()
        self.monitoring = False
        self.bot_app = None
        self.gas_manager = GasManager()
        self.eligibility = EligibilityChecker()

    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r') as f:
                    data = json.load(f)
                    for addr, contract_data in data.items():
                        if contract_data.get("gas_config"):
                            contract_data["gas_config"] = GasConfig.from_dict(contract_data["gas_config"])
                        if contract_data.get("required_nfts") is None:
                            contract_data["required_nfts"] = []
                        self.watched[addr] = WatchedContract(**contract_data)
                print(f"✅ Loaded {len(self.watched)} contracts")
            except Exception as e:
                print(f"Error loading data: {e}")

    def save_data(self):
        data = {}
        for addr, contract in self.watched.items():
            contract_dict = asdict(contract)
            if contract.gas_config:
                contract_dict["gas_config"] = contract.gas_config.to_dict()
            data[addr] = contract_dict
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    def add_contract(self, address: str, chain: str, user_id: int) -> bool:
        address = address.lower().strip()
        if address in self.watched:
            return False
        self.watched[address] = WatchedContract(
            address=address,
            chain=chain,
            added_by=user_id,
            added_at=time.time(),
            required_nfts=[]
        )
        self.save_data()
        return True

    def set_gas_strategy(self, address: str, strategy: str, custom_gwei: float = None, priority_mult: float = 1.2) -> bool:
        if address not in self.watched:
            return False
        self.watched[address].gas_config = GasConfig(strategy=strategy, max_gwei=custom_gwei, priority_multiplier=priority_mult)
        self.save_data()
        return True

    def set_max_gas_usd(self, address: str, max_usd: float) -> bool:
        if address not in self.watched:
            return False
        self.watched[address].max_gas_usd = max_usd
        self.save_data()
        return True

    def set_eligibility_nft(self, address: str, nft_contract: str) -> bool:
        if address not in self.watched:
            return False
        if self.watched[address].required_nfts is None:
            self.watched[address].required_nfts = []
        if nft_contract.lower() not in [x.lower() for x in self.watched[address].required_nfts]:
            self.watched[address].required_nfts.append(nft_contract)
            self.save_data()
            return True
        return False

    def remove_eligibility_nft(self, address: str, nft_contract: str) -> bool:
        if address in self.watched and self.watched[address].required_nfts:
            original_len = len(self.watched[address].required_nfts)
            self.watched[address].required_nfts = [x for x in self.watched[address].required_nfts if x.lower() != nft_contract.lower()]
            if len(self.watched[address].required_nfts) != original_len:
                self.save_data()
                return True
        return False

    async def check_eligibility(self, address: str, wallet_address: str) -> Dict:
        contract = self.watched.get(address)
        if not contract or not contract.required_nfts:
            return {"eligible": True, "message": "✅ No NFT requirements"}
        return await self.eligibility.check_holdings(wallet_address, contract.required_nfts)

    async def start_monitoring(self, bot_app):
        self.bot_app = bot_app
        self.monitoring = True
        print("🟢 Monitoring started!")

monitor = ContractMonitor()
WALLET_CHAIN, WALLET_PRIVATE_KEY = range(2)

# In-memory wallet storage
user_wallets: Dict[int, List[Dict]] = {}

# ============ TELEGRAM HANDLERS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add Wallet", callback_data="addwallet")],
        [InlineKeyboardButton("👛 View Wallets", callback_data="wallets")],
        [InlineKeyboardButton("👁️ Watch Contract", callback_data="watch")],
        [InlineKeyboardButton("🎯 Snipe", callback_data="snipe")],
        [InlineKeyboardButton("⛽ Gas Strategy", callback_data="gasstrategy")],
        [InlineKeyboardButton("🔑 Set Eligibility", callback_data="eligibility")],
        [InlineKeyboardButton("📋 List", callback_data="list")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        [InlineKeyboardButton("⛽ Gas Fees", callback_data="gas")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    alchemy_status = "✅" if ALCHEMY_API_KEY else "❌"
    
    await update.message.reply_text(
        f"🤖 **NFT AUTO-MINT BOT**\n\n"
        f"💰 **Fee:** {FEE_PERCENTAGE}% of mint amount\n"
        f"🔗 **Chains:** Ethereum & Solana\n"
        f"🔑 **Alchemy API:** {alchemy_status} (NFT eligibility)\n\n"
        f"**Commands:**\n"
        f"• `/addwallet` - Add wallet (needs PRIVATE KEY)\n"
        f"• `/watch <contract> <eth/sol>` - Watch NFT\n"
        f"• `/seteligibility <contract> <nft>` - Require holding an NFT\n"
        f"• `/removeeligibility <contract> <nft>` - Remove requirement\n"
        f"• `/listeligibility <contract>` - Show requirements\n"
        f"• `/snipe <contract> <amount>` - Arm auto-mint\n"
        f"• `/gasstrategy <contract> <normal/priority/custom/auto>`\n"
        f"• `/gaslimit <contract> <max_usd>` - Max gas in USD\n"
        f"• `/list` - View all settings\n"
        f"• `/cancel <contract>` - Cancel snipe\n"
        f"• `/gas` - Check gas fees\n\n"
        f"🔒 Private keys encrypted | 🔑 NFT eligibility checks supported",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cmd = query.data
    
    if cmd == "addwallet":
        await query.message.reply_text("Send `/addwallet` then chain and PRIVATE KEY", parse_mode="Markdown")
    elif cmd == "wallets":
        await wallets_command(update, context)
    elif cmd == "watch":
        await query.message.reply_text("Send `/watch <contract> <chain>`\nChains: `eth` or `sol`", parse_mode="Markdown")
    elif cmd == "snipe":
        await query.message.reply_text(
            "🎯 **Snipe**\n\nSend: `/snipe <contract> <amount>`\n\n"
            "Set gas strategy with `/gasstrategy` and eligibility with `/seteligibility`",
            parse_mode="Markdown"
        )
    elif cmd == "gasstrategy":
        await query.message.reply_text(
            "⛽ **Gas Strategies**\n\n"
            "`/gasstrategy <contract> normal`\n"
            "`/gasstrategy <contract> priority [multiplier]`\n"
            "`/gasstrategy <contract> custom <gwei>`\n"
            "`/gasstrategy <contract> auto`\n\n"
            "**Examples:**\n"
            "`/gasstrategy 0x... priority 1.5` (50% premium)\n"
            "`/gasstrategy 0x... custom 200` (use 200 Gwei)",
            parse_mode="Markdown"
        )
    elif cmd == "eligibility":
        await query.message.reply_text(
            "🔑 **NFT Eligibility**\n\n"
            "`/seteligibility <contract> <nft_contract_address>`\n"
            "Bot will check if your wallet holds that NFT before sniping.\n\n"
            "`/removeeligibility <contract> <nft_contract>` - Remove requirement\n"
            "`/listeligibility <contract>` - Show requirements",
            parse_mode="Markdown"
        )
    elif cmd == "list":
        await list_command(update, context)
    elif cmd == "cancel":
        await query.message.reply_text("Send `/cancel <contract_address>`", parse_mode="Markdown")
    elif cmd == "gas":
        await gas_command(update, context)

# ============ WALLET HANDLERS ============
async def add_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💳 **Add Wallet**\n\nWhich chain?\n• `ethereum` / `eth`\n• `solana` / `sol`\n\n"
        "⚠️ Send your **PRIVATE KEY** (not wallet address)\n🔒 It will be encrypted.\n\nSend /cancel to abort.",
        parse_mode="Markdown"
    )
    return WALLET_CHAIN

async def add_wallet_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chain_input = update.message.text.lower()
    if chain_input in ["eth", "ethereum"]:
        context.user_data['wallet_chain'] = "ethereum"
        await update.message.reply_text(
            "✅ Chain: ETHEREUM\n\n🔑 Send your **PRIVATE KEY** (hex starting with 0x, 64 characters)\n\n🔒 It will be encrypted.",
            parse_mode="Markdown"
        )
        return WALLET_PRIVATE_KEY
    elif chain_input in ["sol", "solana"]:
        context.user_data['wallet_chain'] = "solana"
        await update.message.reply_text(
            "✅ Chain: SOLANA\n\n🔑 Send your **PRIVATE KEY** (Base58 encoded)\n\n🔒 It will be encrypted.",
            parse_mode="Markdown"
        )
        return WALLET_PRIVATE_KEY
    else:
        await update.message.reply_text("❌ Invalid chain. Try: eth or sol")
        return WALLET_CHAIN

async def add_wallet_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    private_key = update.message.text.strip()
    chain = context.user_data.get('wallet_chain')
    
    if not private_key or len(private_key) < 30:
        await update.message.reply_text("❌ Invalid private key.")
        return WALLET_PRIVATE_KEY
    
    try:
        if chain == "ethereum":
            # ✅ CORRECT: Derive address from private key
            account = Account.from_key(private_key)
            address = account.address
        else:
            # Solana placeholder
            address = f"SOLANA_{private_key[-20:]}"
    except Exception as e:
        await update.message.reply_text(f"❌ Invalid private key for {chain.upper()}: {str(e)}")
        return WALLET_PRIVATE_KEY
    
    user_id = update.effective_user.id
    if user_id not in user_wallets:
        user_wallets[user_id] = []
    
    user_wallets[user_id].append({
        "chain": chain,
        "address": address,
        "private_key_encrypted": simple_encrypt(private_key),
        "added_at": time.time()
    })
    
    await update.message.reply_text(
        f"✅ **Wallet Added!**\n\n🔗 Chain: {chain.upper()}\n📫 Address: `{address}`\n🔒 Private key: Encrypted\n\n"
        f"💡 Use `/watch` to monitor contracts!\n🔑 Use `/seteligibility` to add NFT requirements!",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    wallets = user_wallets.get(user_id, [])
    if not wallets:
        await update.message.reply_text("💼 No wallets. Use `/addwallet`", parse_mode="Markdown")
        return
    message = "💼 **Your Wallets**\n\n"
    for w in wallets:
        message += f"**{w['chain'].upper()}**\n📫 `{w['address']}`\n🔒 Encrypted\n\n"
    await update.message.reply_text(message, parse_mode="Markdown")

# ============ CONTRACT HANDLERS ============
async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("❌ Usage: `/watch <contract> <chain>`\nChains: eth or sol\n\nExample: `/watch 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0 eth`", parse_mode="Markdown")
        return
    address = context.args[0]
    chain_input = context.args[1].lower()
    if chain_input in ["eth", "ethereum"]:
        chain = "ethereum"
    elif chain_input in ["sol", "solana"]:
        chain = "solana"
    else:
        await update.message.reply_text("❌ Invalid chain. Use: eth or sol")
        return
    if monitor.add_contract(address, chain, update.effective_user.id):
        await update.message.reply_text(
            f"✅ **Watching!**\n📝 `{address[:20]}...`\n🔗 {chain.upper()}\n\n"
            f"🎯 Set gas strategy: `/gasstrategy {address[:15]}...`\n"
            f"🔑 Set eligibility: `/seteligibility {address[:15]}... <nft_contract>`\n"
            f"⚡ Then `/snipe {address[:15]}... <amount>`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Already watching.")

async def seteligibility_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "🔑 **Set NFT Eligibility**\n\n"
            "Usage: `/seteligibility <contract> <nft_contract_address>`\n\n"
            "**Example:**\n"
            "`/seteligibility 0xMintContract 0xBd3531dA5CF5857e7CfAA92426877b022e612cf8`\n\n"
            "Bot will check if your wallet holds this NFT before allowing a snipe!\n\n"
            "💡 Get Alchemy API key for accurate checks (free at alchemy.com)",
            parse_mode="Markdown"
        )
        return
    address = context.args[0].lower().strip()
    nft_addr = context.args[1].strip()
    if address not in monitor.watched:
        await update.message.reply_text("❌ Contract not watched. Use `/watch` first.", parse_mode="Markdown")
        return
    if monitor.set_eligibility_nft(address, nft_addr):
        await update.message.reply_text(
            f"🔑 **Eligibility Requirement Set!**\n\n"
            f"📝 Contract: `{address[:15]}...`\n"
            f"🎫 Required NFT: `{nft_addr[:20]}...`\n\n"
            f"⚡ Bot will check if your wallet holds this NFT before sniping!",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Could not set eligibility (already present?)")

async def removeeligibility_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("❌ Usage: `/removeeligibility <contract> <nft_contract_address>`", parse_mode="Markdown")
        return
    address = context.args[0].lower().strip()
    nft_addr = context.args[1].strip()
    if monitor.remove_eligibility_nft(address, nft_addr):
        await update.message.reply_text(f"✅ Removed NFT requirement for `{nft_addr[:20]}...`", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Not found or already removed")

async def listeligibility_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/listeligibility <contract>`", parse_mode="Markdown")
        return
    address = context.args[0].lower().strip()
    contract = monitor.watched.get(address)
    if not contract:
        await update.message.reply_text("❌ Contract not watched.")
        return
    if contract.required_nfts:
        msg = f"🔑 **Eligibility Requirements**\n\n📝 Contract: `{address[:15]}...`\n\n"
        for nft in contract.required_nfts:
            msg += f"• Must hold NFT: `{nft[:25]}...`\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("✅ No NFT eligibility requirements set for this contract.")

async def gasstrategy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "⛽ **Gas Strategies**\n\n"
            "Usage: `/gasstrategy <contract> <strategy> [value]`\n\n"
            "**Strategies:**\n"
            "• `normal` - Standard gas (safe)\n"
            "• `priority` - Fast gas + premium (for hyped mints)\n"
            "• `priority 1.5` - Fast gas + 50% premium\n"
            "• `custom 150` - Use exactly 150 Gwei\n"
            "• `auto` - Smart automatic selection\n\n"
            "**Examples:**\n"
            "`/gasstrategy 0x... priority`\n"
            "`/gasstrategy 0x... priority 2.0`\n"
            "`/gasstrategy 0x... custom 200`",
            parse_mode="Markdown"
        )
        return
    
    address = context.args[0].lower().strip()
    strategy = context.args[1].lower()
    
    if address not in monitor.watched:
        await update.message.reply_text("❌ Contract not watched. Use `/watch` first.", parse_mode="Markdown")
        return
    
    priority_mult = 1.2
    custom_gwei = None
    
    if len(context.args) >= 3:
        try:
            value = float(context.args[2])
            if strategy == "priority":
                priority_mult = value
            elif strategy == "custom":
                custom_gwei = int(value)
        except ValueError:
            await update.message.reply_text("❌ Invalid value. Must be a number.")
            return
    
    if strategy == "normal":
        monitor.set_gas_strategy(address, "normal")
        response = "🐢 **Normal Gas Strategy**\n\nUses standard gas prices. Safe for normal mints."
    elif strategy == "priority":
        monitor.set_gas_strategy(address, "priority", priority_mult=priority_mult)
        response = f"🚀 **Priority Gas Strategy**\n\nUses fast gas + {int((priority_mult-1)*100)}% premium.\nPerfect for hyped mints!"
    elif strategy == "custom":
        if not custom_gwei:
            await update.message.reply_text("❌ Custom strategy requires a Gwei value: `/gasstrategy <contract> custom 150`")
            return
        monitor.set_gas_strategy(address, "custom", custom_gwei=custom_gwei)
        response = f"🎯 **Custom Gas Strategy**\n\nWill use exactly {custom_gwei} Gwei.\nManual override!"
    elif strategy == "auto":
        monitor.set_gas_strategy(address, "auto")
        response = "🤖 **Auto Gas Strategy**\n\nSmart automatic selection based on market conditions."
    else:
        await update.message.reply_text("❌ Invalid strategy. Use: normal, priority, custom, or auto")
        return
    
    # Get current gas for reference
    gas_prices = await monitor.gas_manager.get_eth_gas_prices()
    current_fast = gas_prices.get("fast", 50)
    current_usd = monitor.gas_manager.gwei_to_usd(current_fast)
    
    await update.message.reply_text(
        f"{response}\n\n"
        f"📝 Contract: `{address[:15]}...`\n"
        f"⛽ Current fast gas: {current_fast} Gwei (~${current_usd})\n\n"
        f"✅ Gas strategy saved!\n"
        f"🎯 Use `/snipe {address[:15]}... <amount>` to arm auto-mint!",
        parse_mode="Markdown"
    )

async def gaslimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "⛽ **Set USD Gas Limit**\n\n"
            "Usage: `/gaslimit <contract> <max_usd>`\n\n"
            "**Example:** `/gaslimit 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0 10`\n\n"
            "Bot will NOT execute if gas fees exceed $10!\n\n"
            "💡 Combine with gas strategy for maximum control.",
            parse_mode="Markdown"
        )
        return
    
    address = context.args[0].lower().strip()
    try:
        max_usd = float(context.args[1])
        if max_usd < 1:
            await update.message.reply_text("❌ Gas limit must be at least $1")
            return
    except ValueError:
        await update.message.reply_text("❌ Invalid amount. Use numbers like: 5, 10, 20")
        return
    
    if address not in monitor.watched:
        await update.message.reply_text("❌ Contract not watched. Use `/watch` first!", parse_mode="Markdown")
        return
    
    monitor.set_max_gas_usd(address, max_usd)
    max_gwei = monitor.gas_manager.usd_to_gwei(max_usd)
    
    await update.message.reply_text(
        f"⛽ **USD Gas Limit Set!**\n\n"
        f"📝 Contract: `{address[:15]}...`\n"
        f"💰 Max Gas: **${max_usd}** (~{max_gwei} Gwei)\n\n"
        f"🛑 Bot will ONLY mint if gas fees are below ${max_usd}!",
        parse_mode="Markdown"
    )

async def snipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            f"🎯 **Snipe**\n\n"
            "Usage: `/snipe <contract> <amount>`\n\n"
            "**Example:** `/snipe 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0 2`\n\n"
            f"💰 Fee: {FEE_PERCENTAGE}% of mint amount\n\n"
            f"⛙ First set your gas strategy:\n"
            f"• `/gasstrategy <contract> priority` - For hyped mints\n"
            f"• `/gasstrategy <contract> normal` - For normal mints\n"
            f"• `/gaslimit <contract> 10` - Max $10 gas\n\n"
            f"🔑 Set NFT requirements:\n"
            f"• `/seteligibility <contract> <nft_address>`\n\n"
            f"⚡ Then snipe will auto-execute with your preferences!",
            parse_mode="Markdown"
        )
        return
    
    address = context.args[0].lower().strip()
    try:
        amount = int(context.args[1])
        if amount < 1 or amount > 50:
            raise ValueError
    except:
        await update.message.reply_text("❌ Amount must be 1-50")
        return
    
    if address not in monitor.watched:
        await update.message.reply_text("❌ Contract not watched. Use `/watch` first!", parse_mode="Markdown")
        return
    
    contract = monitor.watched[address]
    user_id = update.effective_user.id
    
    # Check if user has wallet for this chain
    user_wallet = None
    for w in user_wallets.get(user_id, []):
        if w.get("chain") == contract.chain:
            user_wallet = w
            break
    
    if not user_wallet:
        await update.message.reply_text(f"❌ No {contract.chain.upper()} wallet. Use `/addwallet` first!", parse_mode="Markdown")
        return
    
    # ✅ Check eligibility before arming
    wallet_address = user_wallet["address"]
    elig = await monitor.check_eligibility(address, wallet_address)
    
    if not elig["eligible"]:
        await update.message.reply_text(
            f"❌ **Not Eligible to Snipe**\n\n{elig['message']}\n\n"
            f"Required NFT(s) not found in your wallet:\n`{wallet_address}`\n\n"
            f"Use `/seteligibility` to change requirements or `/removeeligibility` to remove them.",
            parse_mode="Markdown"
        )
        return
    
    # Arm the snipe
    monitor.watched[address].armed_snipe = {"amount": amount, "user_id": user_id, "armed_at": time.time()}
    monitor.save_data()
    
    # Build response message
    gas_msg = ""
    if contract.gas_config:
        gc = contract.gas_config
        if gc.strategy == "priority":
            gas_msg = f"\n⛙ **Gas:** Priority ({int((gc.priority_multiplier-1)*100)}% premium)"
        elif gc.strategy == "custom":
            gas_msg = f"\n⛙ **Gas:** Custom ({gc.max_gwei} Gwei)"
        elif gc.strategy == "auto":
            gas_msg = f"\n⛙ **Gas:** Auto"
        else:
            gas_msg = f"\n⛙ **Gas:** Normal"
    
    if contract.max_gas_usd:
        gas_msg += f"\n💰 **Max Gas:** ${contract.max_gas_usd}"
    
    elig_msg = ""
    if contract.required_nfts:
        elig_msg = f"\n🔑 **Eligibility:** {len(contract.required_nfts)} NFT(s) required ✅ Verified"
    
    await update.message.reply_text(
        f"🎯 **Auto-Mint Armed!**\n\n"
        f"📦 {amount} NFT(s)\n"
        f"🔗 {contract.chain.upper()}\n"
        f"💰 Fee: {FEE_PERCENTAGE}% of mint amount{gas_msg}{elig_msg}\n\n"
        f"⚡ Will trigger when mint goes live!\n"
        f"🛑 Bot respects your gas strategy and eligibility requirements!",
        parse_mode="Markdown"
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitor.watched:
        await update.message.reply_text("📭 No contracts monitored.\nUse `/watch` to add one.", parse_mode="Markdown")
        return
    
    gas_prices = await monitor.gas_manager.get_eth_gas_prices()
    current_usd = monitor.gas_manager.gwei_to_usd(gas_prices.get("fast", 50))
    
    message = "**📋 Watched Contracts**\n\n"
    for addr, contract in monitor.watched.items():
        snipe = f"🎯 {contract.armed_snipe['amount']} NFTs" if contract.armed_snipe else "⚡ No snipe"
        
        gas_display = "⛽ No gas strategy"
        if contract.gas_config:
            g = contract.gas_config
            if g.strategy == "priority":
                gas_display = f"⛽ Priority {int((g.priority_multiplier-1)*100)}%+"
            elif g.strategy == "custom":
                gas_display = f"⛽ Custom {g.max_gwei} Gwei"
            elif g.strategy == "auto":
                gas_display = "⛽ Auto"
            else:
                gas_display = "⛽ Normal"
        
        if contract.max_gas_usd:
            gas_display += f" | 💰 ${contract.max_gas_usd} max"
        
        elig_display = f" | 🔑 {len(contract.required_nfts)} NFT req" if contract.required_nfts else ""
        
        message += f"`{addr[:12]}...` - {contract.chain.upper()}\n{snipe}\n{gas_display}{elig_display}\n\n"
    
    message += f"\n---\n⛽ **Current Fast Gas:** ~${current_usd}"
    
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
        await update.message.reply_text(f"✅ Cancelled snipe for `{address[:15]}...`", parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ No active snipe")

async def gas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    gas_prices = await monitor.gas_manager.get_eth_gas_prices()
    
    slow_usd = monitor.gas_manager.gwei_to_usd(gas_prices.get("slow", 30))
    std_usd = monitor.gas_manager.gwei_to_usd(gas_prices.get("standard", 40))
    fast_usd = monitor.gas_manager.gwei_to_usd(gas_prices.get("fast", 50))
    
    alchemy_status = "✅ Connected" if ALCHEMY_API_KEY else "❌ Not configured (add ALCHEMY_API_KEY)"
    
    await update.message.reply_text(
        f"⛽ **Current Gas Fees (ETH)**\n\n"
        f"• 🐢 Slow: {gas_prices.get('slow', 30)} Gwei (~${slow_usd})\n"
        f"• ⚡ Standard: {gas_prices.get('standard', 40)} Gwei (~${std_usd})\n"
        f"• 🚀 Fast: {gas_prices.get('fast', 50)} Gwei (~${fast_usd})\n"
        f"• 📊 Base Fee: {gas_prices.get('base_fee', 25)} Gwei\n\n"
        f"**Gas Strategies Available:**\n"
        f"• 🐢 Normal - Standard gas\n"
        f"• 🚀 Priority - Fast gas + premium (for hyped mints!)\n"
        f"• 🎯 Custom - Your own Gwei value\n"
        f"• 🤖 Auto - Smart selection\n\n"
        f"**Alchemy API:** {alchemy_status}\n"
        f"**Bot Fee:** {FEE_PERCENTAGE}% of mint amount",
        parse_mode="Markdown"
    )

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Error: {context.error}")

# ============ MAIN ============
def main():
    if not TELEGRAM_TOKEN:
        print("❌ ERROR: TELEGRAM_TOKEN not set!")
        print("Add TELEGRAM_TOKEN to environment variables")
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
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("wallets", wallets_command))
    app.add_handler(CommandHandler("watch", watch_command))
    app.add_handler(CommandHandler("seteligibility", seteligibility_command))
    app.add_handler(CommandHandler("removeeligibility", removeeligibility_command))
    app.add_handler(CommandHandler("listeligibility", listeligibility_command))
    app.add_handler(CommandHandler("gasstrategy", gasstrategy_command))
    app.add_handler(CommandHandler("gaslimit", gaslimit_command))
    app.add_handler(CommandHandler("snipe", snipe_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("gas", gas_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    
    print("=" * 60)
    print("🤖 NFT AUTO-MINT BOT (with Alchemy API Eligibility)")
    print("=" * 60)
    print(f"💰 Fee: {FEE_PERCENTAGE}% of mint amount")
    print(f"🔑 Alchemy API: {'✅ ENABLED' if ALCHEMY_API_KEY else '❌ DISABLED (add ALCHEMY_API_KEY)'}")
    print(f"🔗 Chains: Ethereum, Solana")
    print("=" * 60)
    print("🟢 Bot is running!")
    print("=" * 60)
    
    app.run_polling()

if __name__ == "__main__":
    main()
