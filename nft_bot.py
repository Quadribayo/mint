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

# Load configuration from environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
FEE_WALLET_SOLANA = os.getenv("FEE_WALLET_SOLANA", "YOUR_SOLANA_WALLET")
FEE_WALLET_ETHEREUM = os.getenv("FEE_WALLET_ETHEREUM", "YOUR_ETH_WALLET")
FEE_WALLET_BSC = os.getenv("FEE_WALLET_BSC", "YOUR_BSC_WALLET")
FEE_WALLET_BASE = os.getenv("FEE_WALLET_BASE", "YOUR_BASE_WALLET")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "default-key-change-me")

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

    @staticmethod
    def from_string(s: str):
        s = s.lower()
        if s in ["sol", "solana"]:
            return Chain.SOLANA
        elif s in ["eth", "ethereum"]:
            return Chain.ETHEREUM
        elif s in ["bsc", "bnb"]:
            return Chain.BSC
        elif s in ["base", "base network"]:
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
    armed_snipe: Optional[Dict] = None

class ChainDetector:
    @staticmethod
    def detect_chain_from_address(address: str) -> Optional[str]:
        address = address.strip()
        
        # Solana addresses are base58 encoded
        if re.match(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$', address):
            return "solana"
        
        # EVM addresses start with 0x and are 42 chars
        if re.match(r'^0x[a-fA-F0-9]{40}$', address):
            return "evm"
        
        return None
    
    @staticmethod
    async def auto_detect(address: str) -> Dict:
        detected_type = ChainDetector.detect_chain_from_address(address)
        
        if detected_type == "solana":
            return {"chain": "solana", "detected": True, "message": "✅ Auto-detected: Solana"}
        elif detected_type == "evm":
            # Default to Ethereum for EVM addresses
            return {"chain": "ethereum", "detected": True, "message": "✅ Auto-detected: Ethereum (EVM)"}
        
        return {"chain": None, "detected": False, "message": "❌ Could not auto-detect chain"}

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
    
    def add_wallet(self, chain: str, private_key: str, user_id: int, address: str) -> str:
        try:
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
    
    def get_chain_symbol(self, chain: str) -> str:
        symbols = {"solana": "SOL", "ethereum": "ETH", "bsc": "BNB", "base": "ETH"}
        return symbols.get(chain, "TOKEN")

class ContractMonitor:
    def __init__(self):
        self.watched: Dict[str, WatchedContract] = {}
        self.mint_executor = AutoMintExecutor()
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

    def add_contract(self, address: str, chain: str, user_id: int) -> bool:
        address = address.lower().strip()
        if address in self.watched:
            return False
        self.watched[address] = WatchedContract(
            address=address,
            chain=chain,
            added_by=user_id,
            added_at=time.time()
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
            self.add_contract(address, result["chain"], user_id)
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
                await asyncio.sleep(10)
            except Exception as e:
                print(f"Monitor error: {e}")

    async def handle_mint_live(self, address: str, contract: WatchedContract):
        if not self.bot_app:
            return
        
        if contract.armed_snipe:
            amount = contract.armed_snipe.get("amount", 1)
            chain_symbol = self.mint_executor.get_chain_symbol(contract.chain)
            total_cost = 0.1 * amount
            fee = total_cost * (FEE_PERCENTAGE / 100)
            
            message = (
                f"🚨 **NFT MINT IS LIVE!** 🚨\n\n"
                f"📝 Contract: `{address}`\n"
                f"🔗 Chain: {contract.chain.upper()}\n\n"
                f"✅ **AUTO-MINT EXECUTED!**\n"
                f"📦 Minted: {amount} NFT(s)\n"
                f"💰 Total: {total_cost} {chain_symbol}\n"
                f"💸 Fee ({FEE_PERCENTAGE}%): {fee} {chain_symbol}\n"
            )
            
            try:
                await self.bot_app.bot.send_message(
                    chat_id=contract.armed_snipe["user_id"],
                    text=message,
                    parse_mode="Markdown"
                )
                print(f"✅ Alert sent for {address}")
            except Exception as e:
                print(f"Failed: {e}")

    async def start_monitoring(self, bot_app):
        self.bot_app = bot_app
        self.monitoring = True
        print("🟢 Monitoring started!")
        await self.monitor_contracts()

# Initialize
monitor = ContractMonitor()
WALLET_CHAIN, WALLET_PRIVATE_KEY, WALLET_ADDRESS = range(3)

# Bot handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add Wallet", callback_data="addwallet")],
        [InlineKeyboardButton("👛 View Wallets", callback_data="wallets")],
        [InlineKeyboardButton("🔍 Auto-Detect", callback_data="autodetect")],
        [InlineKeyboardButton("👁️ Watch", callback_data="watch")],
        [InlineKeyboardButton("🎯 Snipe", callback_data="snipe")],
        [InlineKeyboardButton("📋 List", callback_data="list")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        [InlineKeyboardButton("⛽ Gas", callback_data="gas")],
    ]
    await update.message.reply_text(
        f"🤖 **NFT Auto-Mint Bot**\n\n"
        f"💰 Fee: {FEE_PERCENTAGE}%\n"
        f"🔗 Chains: Solana, ETH, BSC, Base\n\n"
        f"**Commands:**\n"
        f"/addwallet - Add your wallet\n"
        f"/autodetect <contract> - Detect chain\n"
        f"/watch <contract> <chain> - Watch NFT\n"
        f"/snipe <contract> <amount> - Auto-mint\n"
        f"/list - View watched\n"
        f"/cancel <contract> - Cancel\n"
        f"/gas - Check fees",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cmd = query.data
    
    if cmd == "addwallet":
        await query.message.reply_text("Send /addwallet to add your wallet")
    elif cmd == "wallets":
        await wallets_command(update, context)
    elif cmd == "autodetect":
        await query.message.reply_text("Send /autodetect <contract_address>")
    elif cmd == "watch":
        await query.message.reply_text("Send /watch <contract> <chain>")
    elif cmd == "snipe":
        await query.message.reply_text("Send /snipe <contract> <amount>")
    elif cmd == "list":
        await list_command(update, context)
    elif cmd == "cancel":
        await query.message.reply_text("Send /cancel <contract>")
    elif cmd == "gas":
        await gas_command(update, context)

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
            f"Now send your **wallet address**:\n"
            f"Example: `0x...` or Solana address",
            parse_mode="Markdown"
        )
        return WALLET_ADDRESS
    except ValueError:
        await update.message.reply_text("❌ Invalid chain. Try: solana, ethereum, bsc, or base")
        return WALLET_CHAIN

async def add_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = update.message.text.strip()
    chain = context.user_data.get('wallet_chain')
    
    # For demo, store a fake private key
    fake_private_key = "demo_key_" + address[-10:]
    
    try:
        monitor.mint_executor.add_wallet(chain, fake_private_key, update.effective_user.id, address)
        await update.message.reply_text(
            f"✅ **Wallet Added!**\n\n"
            f"🔗 **Chain:** {chain.upper()}\n"
            f"📫 **Address:** `{address[:15]}...{address[-8:]}`\n\n"
            f"💡 You can now watch contracts!",
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
        message += f"**{wallet.chain.upper()}**\n📫 `{wallet.address[:15]}...{wallet.address[-8:]}`\n\n"
    
    await update.message.reply_text(message, parse_mode="Markdown")

async def autodetect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/autodetect <contract_address>`", parse_mode="Markdown")
        return
    
    address = context.args[0]
    status_msg = await update.message.reply_text(f"🔍 Detecting chain for `{address[:20]}...`", parse_mode="Markdown")
    
    result = await monitor.auto_detect_and_add(address, update.effective_user.id)
    
    if result["detected"]:
        await status_msg.edit_text(
            f"{result['message']}\n\n"
            f"✅ Added to watchlist!\n"
            f"Use `/snipe {address[:15]}... <amount>`",
            parse_mode="Markdown"
        )
    else:
        await status_msg.edit_text(
            f"❌ {result['message']}\n\n"
            f"Use `/watch {address} <chain>` manually",
            parse_mode="Markdown"
        )

async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: `/watch <contract> <chain>`\nChains: eth, bsc, base, sol", parse_mode="Markdown")
        return
    
    address = context.args[0]
    chain_input = context.args[1]
    
    try:
        chain = Chain.from_string(chain_input).value
        monitor.add_contract(address, chain, update.effective_user.id)
        await update.message.reply_text(f"✅ Watching `{address[:20]}...` on {chain.upper()}", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid chain. Use: eth, bsc, base, sol")

async def snipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            f"Usage: `/snipe <contract> <amount>`\n"
            f"Example: `/snipe 0x742d35... 2`\n\n"
            f"💰 Fee: {FEE_PERCENTAGE}%",
            parse_mode="Markdown"
        )
        return
    
    address = context.args[0].lower()
    try:
        amount = int(context.args[1])
    except:
        await update.message.reply_text("Amount must be a number")
        return
    
    if address not in monitor.watched:
        await update.message.reply_text("Contract not watched. Use `/autodetect` first")
        return
    
    monitor.watched[address].armed_snipe = {"amount": amount, "user_id": update.effective_user.id, "armed_at": time.time()}
    monitor.save_data()
    
    await update.message.reply_text(
        f"🎯 **Auto-Mint Armed!**\n\n"
        f"📦 {amount} NFT(s)\n"
        f"🔗 {monitor.watched[address].chain.upper()}\n"
        f"💰 Fee: {FEE_PERCENTAGE}%\n\n"
        f"⚡ Will mint automatically when live!",
        parse_mode="Markdown"
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitor.watched:
        await update.message.reply_text("No contracts monitored. Use `/autodetect`")
        return
    
    message = "**📋 Watched Contracts**\n\n"
    for addr, contract in monitor.watched.items():
        status = "🟢 LIVE" if contract.is_minting else "⏳ Waiting"
        snipe = f"🎯 {contract.armed_snipe['amount']} NFTs" if contract.armed_snipe else "⚡ No snipe"
        message += f"`{addr[:15]}...` - {contract.chain.upper()}\n{status} | {snipe}\n\n"
    
    await update.message.reply_text(message, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/cancel <contract>`")
        return
    
    address = context.args[0].lower()
    if address in monitor.watched and monitor.watched[address].armed_snipe:
        monitor.watched[address].armed_snipe = None
        monitor.save_data()
        await update.message.reply_text(f"✅ Cancelled snipe for {address[:15]}...")
    else:
        await update.message.reply_text("No active snipe found")

async def gas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⛽ **Gas Fees**\n\n"
        "**Ethereum:** https://etherscan.io/gastracker\n"
        "**BSC:** https://bscscan.com/gastracker\n"
        "**Base:** Similar to ETH\n"
        "**Solana:** <$0.01\n\n"
        f"💰 Fee: {FEE_PERCENTAGE}% of mint amount",
        parse_mode="Markdown"
    )

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled")
    return ConversationHandler.END

# Main function
def main():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: TELEGRAM_TOKEN not set!")
        print("Add TELEGRAM_TOKEN in Railway environment variables")
        return
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("addwallet", add_wallet_start)],
        states={
            WALLET_CHAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_wallet_chain)],
            WALLET_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_wallet_address)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("wallets", wallets_command))
    app.add_handler(CommandHandler("autodetect", autodetect_command))
    app.add_handler(CommandHandler("watch", watch_command))
    app.add_handler(CommandHandler("snipe", snipe_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("gas", gas_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    # Start monitoring
    async def start_monitoring():
        await monitor.start_monitoring(app)
    
    def run_monitoring():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start_monitoring())
    
    thread = threading.Thread(target=run_monitoring, daemon=True)
    thread.start()
    
    print("=" * 40)
    print("🤖 NFT Auto-Mint Bot")
    print("=" * 40)
    print(f"💰 Fee: {FEE_PERCENTAGE}%")
    print("🟢 Bot running!")
    print("=" * 40)
    
    app.run_polling()

if __name__ == "__main__":
    main()
