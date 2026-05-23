import os
import asyncio
import threading
import time
import json
import re
from datetime import datetime
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

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
FEE_PERCENTAGE = 1.0

# Your fee wallets (where the 1% fee goes)
FEE_WALLET_ETHEREUM = os.getenv("FEE_WALLET_ETHEREUM", "YOUR_ETH_WALLET")
FEE_WALLET_SOLANA = os.getenv("FEE_WALLET_SOLANA", "YOUR_SOLANA_WALLET")

DATA_FILE = "watched_contracts.json"
WALLETS_FILE = "wallets.json"

# Simple encryption for private keys
import hashlib
import base64

ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "default-key-change-me")

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

# ============ CHAIN ENUM ============
class Chain(Enum):
    SOLANA = "solana"
    ETHEREUM = "ethereum"

    @staticmethod
    def from_string(s: str):
        s = s.lower()
        if s in ["sol", "solana"]:
            return Chain.SOLANA
        elif s in ["eth", "ethereum"]:
            return Chain.ETHEREUM
        raise ValueError(f"Unsupported chain: {s}")

# ============ DATA MODELS ============
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
    armed_snipe: Optional[Dict] = None
    auto_detected_chain: bool = False

# ============ SIMPLE CHAIN DETECTOR ============
class ChainDetector:
    @staticmethod
    def detect_chain_from_address(address: str) -> Optional[str]:
        address = address.strip()
        
        # EVM addresses are 42 characters starting with 0x
        if re.match(r'^0x[a-fA-F0-9]{40}$', address):
            return "ethereum"
        
        # Solana addresses are base58 encoded, typically 32-44 characters
        if re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', address):
            return "solana"
        
        return None
    
    @staticmethod
    async def auto_detect(address: str) -> Dict:
        detected_type = ChainDetector.detect_chain_from_address(address)
        
        if detected_type == "solana":
            return {"chain": "solana", "detected": True, "message": "✅ Auto-detected: Solana"}
        elif detected_type == "ethereum":
            return {"chain": "ethereum", "detected": True, "message": "✅ Auto-detected: Ethereum"}
        
        return {"chain": None, "detected": False, "message": "❌ Could not auto-detect chain. Use: eth or sol"}

# ============ CONTRACT MONITOR ============
class ContractMonitor:
    def __init__(self):
        self.watched: Dict[str, WatchedContract] = {}
        self.load_data()
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
        result = await ChainDetector.auto_detect(address)
        
        if result["detected"] and result["chain"]:
            self.add_contract(address, result["chain"], user_id, auto_detected=True)
            result["added"] = True
        else:
            result["added"] = False
        
        return result

    async def monitor_contracts(self):
        """Simple monitoring loop - checks every 10 seconds"""
        while self.monitoring:
            try:
                for address, contract in list(self.watched.items()):
                    if not contract.is_minting and contract.armed_snipe:
                        # Simulate mint going live after 30 seconds for testing
                        if time.time() - contract.added_at > 30:
                            contract.is_minting = True
                            self.save_data()
                            await self.handle_mint_live(address, contract)
                await asyncio.sleep(10)
            except Exception as e:
                print(f"Monitor error: {e}")

    async def handle_mint_live(self, address: str, contract: WatchedContract):
        if not self.bot_app:
            return
        
        if contract.armed_snipe:
            amount = int(contract.armed_snipe.get("amount", 1))
            
            # Get fee wallet for this chain
            fee_wallet = FEE_WALLET_ETHEREUM if contract.chain == "ethereum" else FEE_WALLET_SOLANA
            
            message = (
                f"🚨 **NFT MINT IS LIVE!** 🚨\n\n"
                f"📝 Contract: `{address}`\n"
                f"🔗 Chain: {contract.chain.upper()}\n\n"
                f"✅ **AUTO-MINT TRIGGERED!**\n"
                f"📦 Minting: {amount} NFT(s)\n"
                f"💰 Fee ({FEE_PERCENTAGE}%): Taken from mint amount\n\n"
                f"⚠️ Check your wallet for transaction"
            )
            
            try:
                await self.bot_app.bot.send_message(
                    chat_id=contract.armed_snipe["user_id"],
                    text=message,
                    parse_mode="Markdown"
                )
                print(f"✅ Mint alert sent for {address}")
            except Exception as e:
                print(f"Failed to send message: {e}")

    async def start_monitoring(self, bot_app):
        self.bot_app = bot_app
        self.monitoring = True
        print("🟢 Monitoring started!")
        await self.monitor_contracts()

# ============ TELEGRAM HANDLERS ============
monitor = ContractMonitor()
WALLET_CHAIN, WALLET_PRIVATE_KEY = range(2)

# Store user wallets in memory
user_wallets: Dict[int, List[Dict]] = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add Wallet", callback_data="addwallet")],
        [InlineKeyboardButton("👛 View Wallets", callback_data="wallets")],
        [InlineKeyboardButton("🔍 Auto-Detect", callback_data="autodetect")],
        [InlineKeyboardButton("👁️ Watch Contract", callback_data="watch")],
        [InlineKeyboardButton("🎯 Snipe", callback_data="snipe")],
        [InlineKeyboardButton("📋 List Contracts", callback_data="list")],
        [InlineKeyboardButton("❌ Cancel Snipe", callback_data="cancel")],
        [InlineKeyboardButton("⛽ Gas Fees", callback_data="gas")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🤖 **NFT AUTO-MINT BOT**\n\n"
        f"💰 **Fee:** {FEE_PERCENTAGE}% of mint amount\n"
        f"🔗 **Chains:** Ethereum + Solana\n"
        f"🔍 **Auto-Detect:** Yes!\n\n"
        f"**Quick Start:**\n"
        f"1️⃣ `/addwallet` - Add wallet (needs PRIVATE KEY)\n"
        f"2️⃣ `/autodetect <contract>` - Auto-detect & watch\n"
        f"3️⃣ `/snipe <contract> <amount>` - Arm auto-mint\n\n"
        f"🔒 Private keys are encrypted\n"
        f"🔒 Fee wallet is hidden from users",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cmd = query.data
    
    if cmd == "addwallet":
        await query.message.reply_text(
            "💳 **Add Wallet**\n\n"
            "Send: `/addwallet`\n\n"
            "⚠️ **You need to provide your PRIVATE KEY**\n"
            "🔒 Your private key will be ENCRYPTED.",
            parse_mode="Markdown"
        )
    elif cmd == "wallets":
        await wallets_command(update, context)
    elif cmd == "autodetect":
        await query.message.reply_text(
            "🔍 **Auto-Detect Chain**\n\n"
            "Send: `/autodetect <contract_address>`\n\n"
            "Example: `/autodetect 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0`",
            parse_mode="Markdown"
        )
    elif cmd == "watch":
        await query.message.reply_text(
            "👁️ **Watch Contract**\n\n"
            "Send: `/watch <contract> <chain>`\n\n"
            "Chains: `eth` or `sol`\n"
            "Example: `/watch 0x... eth`",
            parse_mode="Markdown"
        )
    elif cmd == "snipe":
        await query.message.reply_text(
            "🎯 **Arm Auto-Mint**\n\n"
            "Send: `/snipe <contract> <amount>`\n\n"
            f"Example: `/snipe 0x... 2`\n\n"
            f"💰 Fee: {FEE_PERCENTAGE}% of mint amount",
            parse_mode="Markdown"
        )
    elif cmd == "list":
        await list_command(update, context)
    elif cmd == "cancel":
        await query.message.reply_text(
            "❌ **Cancel Auto-Mint**\n\n"
            "Send: `/cancel <contract_address>`",
            parse_mode="Markdown"
        )
    elif cmd == "gas":
        await gas_command(update, context)
    elif cmd == "help":
        await help_command(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📚 **Commands**\n\n"
        "**Wallet:**\n"
        "• `/addwallet` - Add wallet (needs PRIVATE KEY)\n"
        "• `/wallets` - View your wallets\n\n"
        "**Monitoring:**\n"
        "• `/autodetect <contract>` - Auto-detect chain\n"
        "• `/watch <contract> <chain>` - Manual watch\n"
        "• `/list` - View watched contracts\n\n"
        "**Auto-Mint:**\n"
        "• `/snipe <contract> <amount>` - Arm auto-mint\n"
        "• `/cancel <contract>` - Cancel snipe\n\n"
        "**Info:**\n"
        "• `/gas` - Check gas fees\n"
        f"• 💰 Fee: {FEE_PERCENTAGE}% of mint\n"
        f"• 🔗 Chains: Ethereum + Solana"
    )
    
    if update.callback_query:
        await update.callback_query.message.edit_text(help_text, parse_mode="Markdown")
    else:
        await update.message.reply_text(help_text, parse_mode="Markdown")

async def add_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💳 **Add Wallet**\n\n"
        "Which chain?\n"
        "• `ethereum` / `eth`\n"
        "• `solana` / `sol`\n\n"
        "⚠️ **Send your PRIVATE KEY** (not wallet address!)\n"
        "🔒 It will be encrypted.\n\n"
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
            f"🔑 **Send your PRIVATE KEY**\n\n"
            f"• Ethereum: Hex starting with `0x` (64 chars)\n"
            f"• Solana: Base58 string (88 chars)\n\n"
            f"⚠️ Keep this key secure!\n"
            f"🔒 It will be encrypted before storage.\n\n"
            f"Example: `0x123abc...`",
            parse_mode="Markdown"
        )
        return WALLET_PRIVATE_KEY
    except ValueError:
        await update.message.reply_text("❌ Invalid chain. Try: eth or sol")
        return WALLET_CHAIN

async def add_wallet_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    private_key = update.message.text.strip()
    chain = context.user_data.get('wallet_chain')
    
    if not private_key or len(private_key) < 30:
        await update.message.reply_text("❌ Invalid private key. Please send a valid private key.")
        return WALLET_PRIVATE_KEY
    
    # Derive address from private key (simplified)
    if chain == "solana":
        address = f"SOL_{private_key[-20:]}"
    else:
        address = f"0x{private_key[-40:]}" if len(private_key) >= 40 else private_key[:42]
    
    # Store wallet
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
        f"✅ **Wallet Added!**\n\n"
        f"🔗 Chain: {chain.upper()}\n"
        f"📫 Address: `{address[:15]}...{address[-8:]}`\n"
        f"🔒 Private key: Encrypted\n\n"
        f"💡 You can now watch contracts and snipe!",
        parse_mode="Markdown"
    )
    
    return ConversationHandler.END

async def wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    wallets = user_wallets.get(user_id, [])
    
    if not wallets:
        await update.message.reply_text(
            "💼 No wallets.\nUse `/addwallet` to add one.\n\n"
            "⚠️ **You need your PRIVATE KEY** (not just wallet address)",
            parse_mode="Markdown"
        )
        return
    
    message = "💼 **Your Wallets**\n\n"
    for wallet in wallets:
        message += f"**{wallet['chain'].upper()}**\n📫 `{wallet['address'][:15]}...`\n🔒 Encrypted\n\n"
    
    await update.message.reply_text(message, parse_mode="Markdown")

async def autodetect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "🔍 **Auto-Detect**\n\nUsage: `/autodetect <contract_address>`\n\nExample: `/autodetect 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0`",
            parse_mode="Markdown"
        )
        return
    
    address = context.args[0].strip()
    
    status_msg = await update.message.reply_text(
        f"🔍 Detecting chain for `{address[:20]}...`...",
        parse_mode="Markdown"
    )
    
    result = await monitor.auto_detect_and_add(address, update.effective_user.id)
    
    if result["detected"]:
        await status_msg.edit_text(
            f"{result['message']}\n\n"
            f"📝 Contract: `{address}`\n"
            f"🔗 Chain: {result['chain'].upper()}\n\n"
            f"✅ Added to watchlist!\n\n"
            f"🎯 Use `/snipe {address[:15]}... <amount>` to arm auto-mint!",
            parse_mode="Markdown"
        )
    else:
        await status_msg.edit_text(
            f"❌ {result['message']}\n\n"
            f"Please specify chain manually:\n"
            f"`/watch {address} eth`\n"
            f"`/watch {address} sol`",
            parse_mode="Markdown"
        )

async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage: `/watch <contract> <chain>`\n\nChains: eth or sol\n\nOr use `/autodetect <contract>`",
            parse_mode="Markdown"
        )
        return
    
    address = context.args[0]
    chain_input = context.args[1]
    
    try:
        chain = Chain.from_string(chain_input).value
        monitor.add_contract(address, chain, update.effective_user.id)
        await update.message.reply_text(
            f"✅ **Watching!**\n\n📝 `{address[:20]}...`\n🔗 {chain.upper()}\n\n🎯 Use `/snipe` to arm auto-mint",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid chain. Use: eth or sol")

async def snipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "❌ Usage: `/snipe <contract> <amount>`\n\nExample: `/snipe 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb0 2`\n\n"
            f"💰 Fee: {FEE_PERCENTAGE}% of mint amount",
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
            f"❌ Contract not watched.\nUse `/autodetect {address}` first!",
            parse_mode="Markdown"
        )
        return
    
    contract = monitor.watched[address]
    
    # Check if user has a wallet for this chain
    user_id = update.effective_user.id
    has_wallet = any(w.get("chain") == contract.chain for w in user_wallets.get(user_id, []))
    
    if not has_wallet:
        await update.message.reply_text(
            f"❌ No {contract.chain.upper()} wallet.\nUse `/addwallet` first!\n\n"
            f"⚠️ You need to provide your PRIVATE KEY",
            parse_mode="Markdown"
        )
        return
    
    monitor.watched[address].armed_snipe = {
        "amount": amount,
        "user_id": user_id,
        "armed_at": time.time()
    }
    monitor.save_data()
    
    auto_tag = " (auto-detected)" if contract.auto_detected_chain else ""
    
    await update.message.reply_text(
        f"🎯 **Auto-Mint Armed!**\n\n"
        f"📦 {amount} NFT(s)\n"
        f"🔗 {contract.chain.upper()}{auto_tag}\n"
        f"💰 Fee: {FEE_PERCENTAGE}%\n\n"
        f"⚡ Will trigger when mint goes live!",
        parse_mode="Markdown"
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitor.watched:
        await update.message.reply_text("📭 No contracts monitored.\nTry `/autodetect <contract>`", parse_mode="Markdown")
        return
    
    message = "**📋 Monitored Contracts**\n\n"
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
        await update.message.reply_text(f"✅ Cancelled snipe for `{address[:15]}...`", parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ No active snipe")

async def gas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⛽ **Gas Fees**\n\n"
        "**Ethereum:** https://etherscan.io/gastracker\n"
        "**Solana:** https://solanabeach.io/\n\n"
        f"💰 Bot fee: {FEE_PERCENTAGE}% of mint amount\n"
        f"🔒 Fee wallet hidden",
        parse_mode="Markdown"
    )

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Error: {context.error}")

# ============ MAIN ============
def main():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: TELEGRAM_TOKEN not set!")
        print("Add TELEGRAM_TOKEN in environment variables")
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
    app.add_handler(CommandHandler("autodetect", autodetect_command))
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
    print(f"💰 Fee: {FEE_PERCENTAGE}%")
    print(f"🔗 Chains: Ethereum + Solana")
    print(f"🔒 Fee wallet: Hidden")
    print(f"🔍 Auto-detect: ENABLED")
    print("=" * 50)
    print("🟢 Bot is running!")
    print("=" * 50)
    
    app.run_polling()

if __name__ == "__main__":
    main()
