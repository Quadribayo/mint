import os
import asyncio
import time
import json
import re
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

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
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")
FEE_PERCENTAGE = 1.0
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "default-key")

DATA_FILE = "watched_projects.json"
WALLETS_FILE = "wallets.json"

OPENSEA_API = "https://api.opensea.io/api/v2"

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
class TrackedProject:
    contract_address: str
    project_name: str
    added_by: int
    added_at: float
    stages: List[Dict]
    armed_snipe: Optional[Dict] = None
    target_offer_eth: Optional[float] = None
    last_floor_price: float = 0
    last_top_offer: float = 0
    last_supply: int = 0

@dataclass
class Wallet:
    address: str
    private_key_encrypted: str
    added_by: int
    added_at: float

# ============ API INTEGRATIONS ============
class OpenSeaAPI:
    @staticmethod
    async def get_collection_stats(contract_address: str) -> Dict:
        try:
            url = f"{OPENSEA_API}/collections/{contract_address}/stats"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {
                            "floor_price": float(data.get("floor_price", 0)),
                            "total_supply": int(data.get("total_supply", 0)),
                            "success": True
                        }
        except:
            pass
        return {"floor_price": 0, "total_supply": 0, "success": False}
    
    @staticmethod
    async def get_top_offer(contract_address: str) -> float:
        try:
            url = f"{OPENSEA_API}/orders/ethereum/seaport/listings"
            params = {"asset_contract_address": contract_address, "limit": 1}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        orders = data.get("orders", [])
                        if orders:
                            return float(orders[0].get("current_price", 0)) / 10**18
        except:
            pass
        return 0

# ============ PROJECT MONITOR ============
class ProjectMonitor:
    def __init__(self):
        self.projects: Dict[str, TrackedProject] = {}
        self.wallets: Dict[str, Wallet] = {}
        self.load_data()
        self.monitoring = False
        self.bot_app = None

    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r') as f:
                    data = json.load(f)
                    for addr, project_data in data.items():
                        self.projects[addr] = TrackedProject(**project_data)
            except:
                pass
        
        if os.path.exists(WALLETS_FILE):
            try:
                with open(WALLETS_FILE, 'r') as f:
                    data = json.load(f)
                    for key, wallet_data in data.items():
                        self.wallets[key] = Wallet(**wallet_data)
            except:
                pass

    def save_data(self):
        data = {addr: asdict(project) for addr, project in self.projects.items()}
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        
        wallet_data = {f"{w.address}": asdict(w) for w in self.wallets.values()}
        with open(WALLETS_FILE, 'w') as f:
            json.dump(wallet_data, f, indent=2)

    def add_project(self, contract_address: str, project_name: str, stages: List[Dict], user_id: int) -> bool:
        contract_address = contract_address.lower().strip()
        if contract_address in self.projects:
            return False
        self.projects[contract_address] = TrackedProject(
            contract_address=contract_address,
            project_name=project_name,
            added_by=user_id,
            added_at=time.time(),
            stages=stages
        )
        self.save_data()
        return True

    def add_wallet(self, private_key: str, user_id: int) -> str:
        try:
            account = Account.from_key(private_key)
            address = account.address
            if address not in self.wallets:
                self.wallets[address] = Wallet(
                    address=address,
                    private_key_encrypted=simple_encrypt(private_key),
                    added_by=user_id,
                    added_at=time.time()
                )
                self.save_data()
            return address
        except Exception as e:
            raise Exception(f"Invalid private key")

    def set_target_offer(self, contract_address: str, target_eth: float) -> bool:
        if contract_address not in self.projects:
            return False
        self.projects[contract_address].target_offer_eth = target_eth
        self.save_data()
        return True

    def arm_snipe(self, contract_address: str, stage_name: str, amount: int, user_id: int) -> bool:
        if contract_address not in self.projects:
            return False
        self.projects[contract_address].armed_snipe = {
            "stage_name": stage_name,
            "amount": amount,
            "user_id": user_id,
            "armed_at": time.time()
        }
        self.save_data()
        return True

    def disarm_snipe(self, contract_address: str) -> bool:
        if contract_address not in self.projects:
            return False
        self.projects[contract_address].armed_snipe = None
        self.save_data()
        return True

    async def update_project_stats(self, contract_address: str) -> Dict:
        if contract_address not in self.projects:
            return {}
        
        stats = await OpenSeaAPI.get_collection_stats(contract_address)
        top_offer = await OpenSeaAPI.get_top_offer(contract_address)
        
        self.projects[contract_address].last_floor_price = stats.get("floor_price", 0)
        self.projects[contract_address].last_top_offer = top_offer
        self.projects[contract_address].last_supply = stats.get("total_supply", 0)
        self.save_data()
        
        target = self.projects[contract_address].target_offer_eth
        if target and top_offer >= target:
            await self.send_offer_alert(contract_address, top_offer, target)
        
        return {
            "floor_price": self.projects[contract_address].last_floor_price,
            "top_offer": self.projects[contract_address].last_top_offer,
            "supply": self.projects[contract_address].last_supply
        }

    async def send_offer_alert(self, contract_address: str, current_offer: float, target: float):
        if not self.bot_app:
            return
        
        project = self.projects[contract_address]
        message = (
            f"🎯 **TARGET HIT!** 🎯\n\n"
            f"📊 **Project:** {project.project_name}\n"
            f"💰 **Current Top Offer:** {current_offer} ETH\n"
            f"🎯 **Your Target:** {target} ETH\n\n"
            f"✅ Time to consider selling!"
        )
        
        try:
            await self.bot_app.bot.send_message(
                chat_id=project.added_by,
                text=message,
                parse_mode="Markdown"
            )
        except:
            pass

    async def monitor_loop(self):
        while self.monitoring:
            for contract_addr, project in self.projects.items():
                await self.update_project_stats(contract_addr)
            await asyncio.sleep(30)

    async def start_monitoring(self, bot_app):
        self.bot_app = bot_app
        self.monitoring = True
        print("🟢 Monitoring started!")
        await self.monitor_loop()

# ============ TELEGRAM HANDLERS ============
monitor = ProjectMonitor()
ADD_WALLET = 0

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add Wallet", callback_data="add_wallet")],
        [InlineKeyboardButton("👛 My Wallets", callback_data="view_wallets")],
        [InlineKeyboardButton("📊 Track Project", callback_data="track_project")],
        [InlineKeyboardButton("🎯 Set Target", callback_data="set_target")],
        [InlineKeyboardButton("🎯 Auto-Mint", callback_data="arm_snipe")],
        [InlineKeyboardButton("📋 My Projects", callback_data="list_projects")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_snipe")],
        [InlineKeyboardButton("⛽ Gas", callback_data="gas")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🤖 **NFT SNIPER BOT**\n\n"
        f"💰 **Fee:** {FEE_PERCENTAGE}%\n"
        f"🔗 **Chain:** Ethereum\n\n"
        f"**Features:**\n"
        f"• 🎟️ GTD, WL, FCFS, Presale minting\n"
        f"• 📊 Live floor price & offers\n"
        f"• 🎯 Target price alerts\n"
        f"• 🚀 Auto-mint for any stage\n\n"
        f"**Quick Start:**\n"
        f"1️⃣ `/addwallet` - Add your wallet\n"
        f"2️⃣ `/track` - Start tracking a project\n"
        f"3️⃣ `/snipe` - Arm auto-mint\n\n"
        f"📝 Type /help for all commands",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📚 **Available Commands**\n\n"
        "**Wallet**\n"
        "• `/addwallet` - Add your wallet\n"
        "• `/wallets` - View your wallets\n\n"
        "**Tracking**\n"
        "• `/track <contract> <name>` - Track a project\n"
        "• `/addstage <contract> <stage> <price> <max>` - Add mint stage\n"
        "• `/stages <contract>` - View stages\n"
        "• `/list` - View tracked projects\n\n"
        "**Alerts & Auto-Mint**\n"
        "• `/settarget <contract> <eth>` - Set price alert\n"
        "• `/snipe <contract> <stage> <amount>` - Arm auto-mint\n"
        "• `/cancel <contract>` - Cancel auto-mint\n\n"
        "**Info**\n"
        "• `/stats <contract>` - Live project stats\n"
        "• `/refresh <contract>` - Update stats\n"
        "• `/gas` - Check gas fees\n\n"
        f"💰 **Fee:** {FEE_PERCENTAGE}% of mint amount"
    )
    
    if update.callback_query:
        await update.callback_query.message.edit_text(help_text, parse_mode="Markdown")
    else:
        await update.message.reply_text(help_text, parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cmd = query.data
    
    if cmd == "add_wallet":
        await query.message.reply_text("Send `/addwallet` then your private key", parse_mode="Markdown")
    elif cmd == "view_wallets":
        await wallets_command(update, context)
    elif cmd == "track_project":
        await query.message.reply_text(
            "📊 **Track a Project**\n\n"
            "Send: `/track <contract> <name>`\n\n"
            "Example: `/track 0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D BAYC`",
            parse_mode="Markdown"
        )
    elif cmd == "set_target":
        await query.message.reply_text(
            "🎯 **Set Price Alert**\n\n"
            "Send: `/settarget <contract> <eth>`\n\n"
            "Example: `/settarget 0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D 5`",
            parse_mode="Markdown"
        )
    elif cmd == "arm_snipe":
        await query.message.reply_text(
            "🎯 **Auto-Mint**\n\n"
            "Send: `/snipe <contract> <stage> <amount>`\n\n"
            "Stages: GTD, WL, FCFS, Presale\n\n"
            "Example: `/snipe 0x... WL 2`",
            parse_mode="Markdown"
        )
    elif cmd == "list_projects":
        await list_command(update, context)
    elif cmd == "refresh":
        await query.message.reply_text("Send `/refresh <contract>` to update stats", parse_mode="Markdown")
    elif cmd == "cancel_snipe":
        await query.message.reply_text("Send `/cancel <contract>` to cancel", parse_mode="Markdown")
    elif cmd == "gas":
        await gas_command(update, context)

async def add_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💳 **Add Wallet**\n\n"
        "Send your private key (starts with 0x)\n🔒 It will be encrypted.\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown"
    )
    return ADD_WALLET

async def add_wallet_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    private_key = update.message.text.strip()
    
    if not private_key or len(private_key) < 30:
        await update.message.reply_text("❌ Invalid private key.")
        return ADD_WALLET
    
    try:
        address = monitor.add_wallet(private_key, update.effective_user.id)
        await update.message.reply_text(
            f"✅ **Wallet Added!**\n\n"
            f"📫 Address: `{address[:15]}...{address[-8:]}`\n\n"
            f"💡 Use `/track` to start monitoring projects!",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to add wallet. Please check your private key.")
    
    return ConversationHandler.END

async def wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_wallets = [w for w in monitor.wallets.values() if w.added_by == update.effective_user.id]
    
    if not user_wallets:
        await update.message.reply_text("💼 No wallets. Use `/addwallet`", parse_mode="Markdown")
        return
    
    message = "💼 **Your Wallets**\n\n"
    for w in user_wallets:
        message += f"📫 `{w.address[:15]}...{w.address[-8:]}`\n\n"
    
    await update.message.reply_text(message, parse_mode="Markdown")

async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "📊 **Track a Project**\n\n"
            "Usage: `/track <contract> <name>`\n\n"
            "Example: `/track 0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D BAYC`",
            parse_mode="Markdown"
        )
        return
    
    contract = context.args[0].strip()
    project_name = " ".join(context.args[1:])
    
    if monitor.add_project(contract, project_name, [], update.effective_user.id):
        await update.message.reply_text(
            f"✅ **Tracking {project_name}!**\n\n"
            f"📝 Contract: `{contract[:15]}...`\n\n"
            f"📊 Add mint stages with `/addstage {contract[:15]}... <stage> <price> <max>`\n\n"
            "**Stages:** GTD, WL, FCFS, Presale\n\n"
            f"Example: `/addstage {contract[:15]}... WL 0.08 2`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Already tracking this contract.")

async def addstage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text(
            "📊 **Add Mint Stage**\n\n"
            "Usage: `/addstage <contract> <stage> <price> <max>`\n\n"
            "Stages: GTD, WL, FCFS, Presale\n\n"
            "Example: `/addstage 0x... WL 0.08 2`",
            parse_mode="Markdown"
        )
        return
    
    contract = context.args[0].lower().strip()
    stage_name = context.args[1].upper()
    try:
        price = float(context.args[2])
        max_per_wallet = int(context.args[3])
    except:
        await update.message.reply_text("❌ Invalid price or max amount")
        return
    
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked. Use `/track` first.", parse_mode="Markdown")
        return
    
    new_stage = {
        "name": stage_name,
        "price_eth": price,
        "max_per_wallet": max_per_wallet,
        "is_active": False,
        "is_minted": False
    }
    
    monitor.projects[contract].stages.append(new_stage)
    monitor.save_data()
    
    await update.message.reply_text(
        f"✅ **Stage Added!**\n\n"
        f"📊 {monitor.projects[contract].project_name}\n"
        f"🎟️ {stage_name} | {price} ETH | Max {max_per_wallet}\n\n"
        f"🎯 Use `/snipe` to arm auto-mint!",
        parse_mode="Markdown"
    )

async def set_target_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "🎯 **Set Price Alert**\n\n"
            "Usage: `/settarget <contract> <eth>`\n\n"
            "Example: `/settarget 0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D 5`",
            parse_mode="Markdown"
        )
        return
    
    contract = context.args[0].lower().strip()
    try:
        target = float(context.args[1])
    except:
        await update.message.reply_text("❌ Target must be a number")
        return
    
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked.", parse_mode="Markdown")
        return
    
    monitor.set_target_offer(contract, target)
    
    await update.message.reply_text(
        f"🎯 **Alert Set!**\n\n"
        f"📊 {monitor.projects[contract].project_name}\n"
        f"💰 Target: {target} ETH\n\n"
        f"You'll be notified when offers hit this price!",
        parse_mode="Markdown"
    )

async def snipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "🎯 **Auto-Mint**\n\n"
            "Usage: `/snipe <contract> <stage> <amount>`\n\n"
            "Stages: GTD, WL, FCFS, Presale\n\n"
            "Example: `/snipe 0x... WL 2`",
            parse_mode="Markdown"
        )
        return
    
    contract = context.args[0].lower().strip()
    stage = context.args[1].upper()
    try:
        amount = int(context.args[2])
        if amount < 1 or amount > 50:
            raise ValueError
    except:
        await update.message.reply_text("❌ Amount must be 1-50")
        return
    
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked.", parse_mode="Markdown")
        return
    
    stage_exists = any(s.get("name", "").upper() == stage for s in monitor.projects[contract].stages)
    if not stage_exists:
        await update.message.reply_text(f"❌ Stage '{stage}' not found. Add it with `/addstage`", parse_mode="Markdown")
        return
    
    has_wallet = any(w.added_by == update.effective_user.id for w in monitor.wallets.values())
    if not has_wallet:
        await update.message.reply_text("❌ No wallet. Use `/addwallet` first!", parse_mode="Markdown")
        return
    
    monitor.arm_snipe(contract, stage, amount, update.effective_user.id)
    
    await update.message.reply_text(
        f"🎯 **Auto-Mint Armed!**\n\n"
        f"📊 {monitor.projects[contract].project_name}\n"
        f"🎟️ {stage} Stage\n"
        f"📦 {amount} NFT(s)\n"
        f"💰 Fee: {FEE_PERCENTAGE}%\n\n"
        f"⚡ Will mint automatically when {stage} goes live!",
        parse_mode="Markdown"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/stats <contract>`", parse_mode="Markdown")
        return
    
    contract = context.args[0].lower().strip()
    
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked.", parse_mode="Markdown")
        return
    
    msg = await update.message.reply_text("🔄 Fetching latest data...", parse_mode="Markdown")
    
    stats = await monitor.update_project_stats(contract)
    project = monitor.projects[contract]
    
    stages_text = ""
    for stage in project.stages:
        stages_text += f"• **{stage.get('name')}** - {stage.get('price_eth')} ETH (max {stage.get('max_per_wallet')})\n"
    
    target_text = f"🎯 Target: {project.target_offer_eth} ETH" if project.target_offer_eth else "🎯 No target set"
    
    await msg.edit_text(
        f"📊 **{project.project_name}**\n\n"
        f"💰 Floor Price: {stats.get('floor_price', 0)} ETH\n"
        f"💎 Top Offer: {stats.get('top_offer', 0)} ETH\n"
        f"📦 Supply: {stats.get('supply', 0)}\n"
        f"{target_text}\n\n"
        f"**Mint Stages:**\n{stages_text}\n"
        f"🎯 Auto-mint: {'✅ Armed' if project.armed_snipe else '❌ Not armed'}",
        parse_mode="Markdown"
    )

async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/refresh <contract>`", parse_mode="Markdown")
        return
    
    contract = context.args[0].lower().strip()
    
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked.", parse_mode="Markdown")
        return
    
    msg = await update.message.reply_text("🔄 Refreshing...", parse_mode="Markdown")
    
    stats = await monitor.update_project_stats(contract)
    
    await msg.edit_text(
        f"✅ **Updated!**\n\n"
        f"📊 {monitor.projects[contract].project_name}\n"
        f"💰 Floor: {stats.get('floor_price', 0)} ETH\n"
        f"💎 Top Offer: {stats.get('top_offer', 0)} ETH\n"
        f"📦 Supply: {stats.get('supply', 0)}",
        parse_mode="Markdown"
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitor.projects:
        await update.message.reply_text("📭 No projects tracked.", parse_mode="Markdown")
        return
    
    message = "**📋 Tracked Projects**\n\n"
    for addr, project in monitor.projects.items():
        snipe = "🔫 Armed" if project.armed_snipe else "⚡ Watching"
        message += f"**{project.project_name}**\n`{addr[:12]}...` | {snipe}\n\n"
    
    await update.message.reply_text(message, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/cancel <contract>`", parse_mode="Markdown")
        return
    
    contract = context.args[0].lower().strip()
    
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked.")
        return
    
    if monitor.projects[contract].armed_snipe:
        monitor.disarm_snipe(contract)
        await update.message.reply_text(f"✅ Cancelled auto-mint", parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ No active auto-mint")

async def stages_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/stages <contract>`", parse_mode="Markdown")
        return
    
    contract = context.args[0].lower().strip()
    
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked.", parse_mode="Markdown")
        return
    
    project = monitor.projects[contract]
    
    if not project.stages:
        await update.message.reply_text(f"No stages added yet. Use `/addstage`", parse_mode="Markdown")
        return
    
    message = f"🎟️ **Mint Stages for {project.project_name}**\n\n"
    for stage in project.stages:
        message += f"**{stage.get('name')}**\n💰 {stage.get('price_eth')} ETH\n👥 Max {stage.get('max_per_wallet')}\n\n"
    
    await update.message.reply_text(message, parse_mode="Markdown")

async def gas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        response = requests.get("https://api.etherscan.io/api?module=gastracker&action=gasoracle", timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "1":
                gas = data["result"]
                await update.message.reply_text(
                    f"⛽ **Current Gas**\n\n"
                    f"🐢 Slow: {gas['SafeGasPrice']} Gwei\n"
                    f"⚡ Standard: {gas['ProposeGasPrice']} Gwei\n"
                    f"🚀 Fast: {gas['FastGasPrice']} Gwei\n\n"
                    f"💰 Fee: {FEE_PERCENTAGE}%",
                    parse_mode="Markdown"
                )
                return
    except:
        pass
    
    await update.message.reply_text(
        f"⛽ Check gas at https://etherscan.io/gastracker\n\n💰 Fee: {FEE_PERCENTAGE}%",
        parse_mode="Markdown"
    )

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ============ MAIN ============
def main():
    if not TELEGRAM_TOKEN:
        print("❌ ERROR: TELEGRAM_TOKEN not set!")
        return
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    wallet_conv = ConversationHandler(
        entry_points=[CommandHandler("addwallet", add_wallet_start)],
        states={ADD_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_wallet_key)]},
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(wallet_conv)
    app.add_handler(CommandHandler("wallets", wallets_command))
    app.add_handler(CommandHandler("track", track_command))
    app.add_handler(CommandHandler("addstage", addstage_command))
    app.add_handler(CommandHandler("stages", stages_command))
    app.add_handler(CommandHandler("settarget", set_target_command))
    app.add_handler(CommandHandler("snipe", snipe_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("gas", gas_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    async def start_monitoring():
        await monitor.start_monitoring(app)
    
    def run_monitoring():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start_monitoring())
    
    import threading
    monitor_thread = threading.Thread(target=run_monitoring, daemon=True)
    monitor_thread.start()
    
    print("=" * 50)
    print("🤖 NFT SNIPER BOT")
    print("=" * 50)
    print(f"💰 Fee: {FEE_PERCENTAGE}%")
    print("🎟️ Stages: GTD, WL, FCFS, Presale")
    print("🟢 Bot is running!")
    print("=" * 50)
    
    app.run_polling()

if __name__ == "__main__":
    main()
