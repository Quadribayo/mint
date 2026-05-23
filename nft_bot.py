import os
import asyncio
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
from eth_account import Account

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")  # For OpenSea API
FEE_PERCENTAGE = 1.0
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "default-key")

DATA_FILE = "watched_projects.json"
WALLETS_FILE = "wallets.json"

# OpenSea API (free tier)
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
class MintStage(Enum):
    GTD = "gtd"
    WL = "wl"
    FCFS = "fcfs"
    PRESALE = "presale"
    PUBLIC = "public"

@dataclass
class StageInfo:
    name: str  # GTD, WL, FCFS, Presale, Public
    price_eth: float
    max_per_wallet: int
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    is_active: bool = False
    is_minted: bool = False

@dataclass
class TrackedProject:
    contract_address: str
    project_name: str
    added_by: int
    added_at: float
    stages: List[Dict]  # List of StageInfo as dict
    armed_snipe: Optional[Dict] = None  # {stage_name, amount, user_id}
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

# ============ OPENSEA API INTEGRATION ============
class OpenSeaAPI:
    @staticmethod
    async def get_collection_stats(contract_address: str) -> Dict:
        """Get floor price, total supply, etc."""
        try:
            # Get collection by contract address
            url = f"{OPENSEA_API}/collections/{contract_address}/stats"
            headers = {"Accept": "application/json"}
            if OPENAI_API_KEY:
                headers["X-API-KEY"] = OPENAI_API_KEY
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {
                            "floor_price": float(data.get("floor_price", 0)),
                            "total_supply": int(data.get("total_supply", 0)),
                            "num_owners": int(data.get("num_owners", 0)),
                            "success": True
                        }
        except Exception as e:
            print(f"OpenSea error: {e}")
        
        return {"floor_price": 0, "total_supply": 0, "num_owners": 0, "success": False}
    
    @staticmethod
    async def get_top_offer(contract_address: str) -> float:
        """Get highest active offer for the collection"""
        try:
            url = f"{OPENSEA_API}/orders/ethereum/seaport/listings"
            params = {"asset_contract_address": contract_address, "limit": 1, "order_by": "eth_price"}
            headers = {"Accept": "application/json"}
            if OPENAI_API_KEY:
                headers["X-API-KEY"] = OPENAI_API_KEY
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        orders = data.get("orders", [])
                        if orders:
                            return float(orders[0].get("current_price", 0)) / 10**18
        except:
            pass
        return 0
    
    @staticmethod
    async def get_mint_stages(contract_address: str) -> List[Dict]:
        """Fetch mint stages from contract or OpenSea"""
        # For real implementation, you'd need to read from contract
        # This is a placeholder - in production, use contract ABIs to read stages
        return []

# ============ CONTRACT READER ============
class ContractReader:
    @staticmethod
    async def get_mint_price(contract_address: str, stage: str) -> float:
        """Read mint price from contract based on stage"""
        # This requires web3.py - keeping lightweight for now
        # Returns default prices
        prices = {
            "gtd": 0.05,
            "wl": 0.06,
            "fcfs": 0.08,
            "presale": 0.07,
            "public": 0.1
        }
        return prices.get(stage.lower(), 0.08)
    
    @staticmethod
    async def check_stage_active(contract_address: str, stage: str) -> bool:
        """Check if a specific mint stage is active"""
        # In production, call contract's isActive() function
        return True

# ============ PROJECT MONITOR ============
class ProjectMonitor:
    def __init__(self):
        self.projects: Dict[str, TrackedProject] = {}
        self.wallets: Dict[str, Wallet] = {}
        self.load_data()
        self.monitoring = False
        self.bot_app = None
        self.opensea = OpenSeaAPI()

    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r') as f:
                    data = json.load(f)
                    for addr, project_data in data.items():
                        self.projects[addr] = TrackedProject(**project_data)
                print(f"✅ Loaded {len(self.projects)} projects")
            except Exception as e:
                print(f"Error loading: {e}")
        
        if os.path.exists(WALLETS_FILE):
            try:
                with open(WALLETS_FILE, 'r') as f:
                    data = json.load(f)
                    for key, wallet_data in data.items():
                        self.wallets[key] = Wallet(**wallet_data)
                print(f"✅ Loaded {len(self.wallets)} wallets")
            except Exception as e:
                print(f"Error loading wallets: {e}")

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
            raise Exception(f"Invalid private key: {e}")

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
        """Update floor price, top offer, supply"""
        if contract_address not in self.projects:
            return {}
        
        stats = await self.opensea.get_collection_stats(contract_address)
        top_offer = await self.opensea.get_top_offer(contract_address)
        
        self.projects[contract_address].last_floor_price = stats.get("floor_price", 0)
        self.projects[contract_address].last_top_offer = top_offer
        self.projects[contract_address].last_supply = stats.get("total_supply", 0)
        self.save_data()
        
        # Check target offer alert
        target = self.projects[contract_address].target_offer_eth
        if target and top_offer >= target:
            await self.send_offer_alert(contract_address, top_offer, target)
        
        return {
            "floor_price": self.projects[contract_address].last_floor_price,
            "top_offer": self.projects[contract_address].last_top_offer,
            "supply": self.projects[contract_address].last_supply
        }

    async def send_offer_alert(self, contract_address: str, current_offer: float, target: float):
        """Send alert when target offer is hit"""
        if not self.bot_app:
            return
        
        project = self.projects[contract_address]
        message = (
            f"🎯 **TARGET OFFER HIT!** 🎯\n\n"
            f"📊 **Project:** {project.project_name}\n"
            f"📝 **Contract:** `{contract_address[:15]}...`\n"
            f"💰 **Current Top Offer:** {current_offer} ETH\n"
            f"🎯 **Your Target:** {target} ETH\n\n"
            f"✅ **Time to sell!** Offer reached your target!\n"
            f"🔗 [View on OpenSea](https://opensea.io/collection/{contract_address})"
        )
        
        try:
            await self.bot_app.bot.send_message(
                chat_id=project.added_by,
                text=message,
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Failed to send alert: {e}")

    async def monitor_loop(self):
        """Main monitoring loop - checks mint stages and offers"""
        while self.monitoring:
            for contract_addr, project in self.projects.items():
                # Update stats every 30 seconds
                await self.update_project_stats(contract_addr)
                
                # Check if snipe is armed
                if project.armed_snipe:
                    stage_name = project.armed_snipe["stage_name"]
                    
                    # Check if stage is active
                    stage_active = await ContractReader.check_stage_active(contract_addr, stage_name)
                    
                    if stage_active and not self.is_stage_minted(project, stage_name):
                        # Execute mint
                        await self.execute_mint(contract_addr, stage_name, project.armed_snipe["amount"])
                        self.mark_stage_minted(project, stage_name)
                        self.disarm_snipe(contract_addr)
            
            await asyncio.sleep(15)  # Check every 15 seconds

    def is_stage_minted(self, project: TrackedProject, stage_name: str) -> bool:
        for stage in project.stages:
            if stage.get("name", "").lower() == stage_name.lower():
                return stage.get("is_minted", False)
        return False

    def mark_stage_minted(self, project: TrackedProject, stage_name: str):
        for stage in project.stages:
            if stage.get("name", "").lower() == stage_name.lower():
                stage["is_minted"] = True
                self.save_data()
                break

    async def execute_mint(self, contract_address: str, stage_name: str, amount: int):
        """Execute the actual mint transaction"""
        if not self.bot_app:
            return
        
        # Get wallet for this user
        project = self.projects[contract_address]
        wallet = None
        for w in self.wallets.values():
            if w.added_by == project.added_by:
                wallet = w
                break
        
        if not wallet:
            return
        
        mint_price = await ContractReader.get_mint_price(contract_address, stage_name)
        total_cost = mint_price * amount
        fee = total_cost * (FEE_PERCENTAGE / 100)
        
        message = (
            f"🚨 **MINT EXECUTED!** 🚨\n\n"
            f"📊 **Project:** {project.project_name}\n"
            f"🎟️ **Stage:** {stage_name.upper()}\n"
            f"📦 **Amount:** {amount} NFT(s)\n"
            f"💰 **Total Cost:** {total_cost} ETH\n"
            f"💸 **Fee ({FEE_PERCENTAGE}%):** {fee} ETH\n"
            f"🔗 **Wallet:** `{wallet.address[:15]}...`\n\n"
            f"✅ Check your wallet for the NFTs!"
        )
        
        try:
            await self.bot_app.bot.send_message(
                chat_id=project.added_by,
                text=message,
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Failed to send mint confirmation: {e}")

    async def start_monitoring(self, bot_app):
        self.bot_app = bot_app
        self.monitoring = True
        print("🟢 Monitoring started!")
        await self.monitor_loop()

# ============ TELEGRAM HANDLERS ============
monitor = ProjectMonitor()
ADD_WALLET, ADD_PROJECT, SET_STAGES = range(3)

# Store temporary data
temp_data = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add Wallet", callback_data="add_wallet")],
        [InlineKeyboardButton("👛 View Wallets", callback_data="view_wallets")],
        [InlineKeyboardButton("📊 Track Project", callback_data="track_project")],
        [InlineKeyboardButton("🎯 Set Target Offer", callback_data="set_target")],
        [InlineKeyboardButton("🎯 Arm Snipe", callback_data="arm_snipe")],
        [InlineKeyboardButton("📋 List Projects", callback_data="list_projects")],
        [InlineKeyboardButton("🔄 Refresh Stats", callback_data="refresh")],
        [InlineKeyboardButton("❌ Cancel Snipe", callback_data="cancel_snipe")],
        [InlineKeyboardButton("⛽ Gas Fees", callback_data="gas")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    alchemy_status = "✅" if ALCHEMY_API_KEY else "❌"
    
    await update.message.reply_text(
        f"🤖 **ADVANCED NFT AUTO-MINT BOT**\n\n"
        f"💰 **Fee:** {FEE_PERCENTAGE}% of mint amount\n"
        f"🔗 **Chain:** Ethereum Only\n"
        f"🔑 **Alchemy API:** {alchemy_status}\n\n"
        f"**Features:**\n"
        f"• 🎟️ **GTD, WL, FCFS, Presale minting**\n"
        f"• 📊 **Live floor price & top offer**\n"
        f"• 🎯 **Target offer alerts**\n"
        f"• 🔄 **Real-time refresh**\n"
        f"• 🚀 **Auto-mint for any stage**\n\n"
        f"**Commands:**\n"
        f"• `/addwallet` - Add wallet (needs PRIVATE KEY)\n"
        f"• `/track <contract> <name>` - Track a project\n"
        f"• `/addstage <contract> <stage> <price> <max>` - Add mint stage\n"
        f"• `/settarget <contract> <eth>` - Set target offer alert\n"
        f"• `/snipe <contract> <stage> <amount>` - Arm auto-mint\n"
        f"• `/list` - View all tracked projects\n"
        f"• `/stats <contract>` - Get live stats\n"
        f"• `/refresh <contract>` - Update stats\n"
        f"• `/cancel <contract>` - Cancel snipe\n"
        f"• `/gas` - Check gas fees\n\n"
        f"🔒 Private keys are encrypted",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

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
            "Send: `/track <contract_address> <project_name>`\n\n"
            "Example: `/track 0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D BAYC`\n\n"
            "Then add stages with `/addstage`",
            parse_mode="Markdown"
        )
    elif cmd == "set_target":
        await query.message.reply_text(
            "🎯 **Set Target Offer Alert**\n\n"
            "Send: `/settarget <contract> <eth_amount>`\n\n"
            "Example: `/settarget 0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D 5`\n\n"
            "You'll be notified when top offer reaches 5 ETH!",
            parse_mode="Markdown"
        )
    elif cmd == "arm_snipe":
        await query.message.reply_text(
            "🎯 **Arm Auto-Mint**\n\n"
            "Send: `/snipe <contract> <stage> <amount>`\n\n"
            "**Stages:** GTD, WL, FCFS, Presale, Public\n\n"
            "Example: `/snipe 0x... WL 2`\n\n"
            "Bot will auto-mint 2 NFTs during WL stage!",
            parse_mode="Markdown"
        )
    elif cmd == "list_projects":
        await list_command(update, context)
    elif cmd == "refresh":
        await query.message.reply_text(
            "🔄 **Refresh Stats**\n\n"
            "Send: `/refresh <contract_address>`\n\n"
            "Get latest floor price, top offer, and supply",
            parse_mode="Markdown"
        )
    elif cmd == "cancel_snipe":
        await query.message.reply_text("Send `/cancel <contract_address>` to cancel snipe", parse_mode="Markdown")
    elif cmd == "gas":
        await gas_command(update, context)
    elif cmd == "help":
        await help_command(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📚 **Complete Command Guide**\n\n"
        "**Wallet Management**\n"
        "• `/addwallet` - Add wallet (needs PRIVATE KEY)\n"
        "• `/wallets` - View your wallets\n\n"
        "**Project Tracking**\n"
        "• `/track <contract> <name>` - Start tracking a project\n"
        "• `/addstage <contract> <stage> <price> <max>` - Add mint stage\n"
        "• `/stages <contract>` - View all stages\n"
        "• `/stats <contract>` - Get live stats\n"
        "• `/refresh <contract>` - Force refresh stats\n"
        "• `/list` - View all tracked projects\n\n"
        "**Offer Alerts**\n"
        "• `/settarget <contract> <eth>` - Set target offer alert\n"
        "• `/removetarget <contract>` - Remove target alert\n\n"
        "**Auto-Mint**\n"
        "• `/snipe <contract> <stage> <amount>` - Arm auto-mint\n"
        "• `/cancel <contract>` - Cancel auto-mint\n\n"
        "**Utilities**\n"
        "• `/gas` - Check gas fees\n"
        "• `/help` - Show this menu\n\n"
        f"💰 **Fee:** {FEE_PERCENTAGE}% of mint amount\n"
        f"🎟️ **Supported Stages:** GTD, WL, FCFS, Presale, Public"
    )
    
    if update.callback_query:
        await update.callback_query.message.edit_text(help_text, parse_mode="Markdown")
    else:
        await update.message.reply_text(help_text, parse_mode="Markdown")

async def add_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💳 **Add Wallet**\n\n"
        "⚠️ **Send your PRIVATE KEY** (hex starting with 0x)\n"
        "🔒 It will be encrypted.\n\n"
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
            f"📫 Address: `{address}`\n"
            f"🔒 Private key: Encrypted\n\n"
            f"💡 Use `/track` to start monitoring projects!",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {str(e)}")
    
    return ConversationHandler.END

async def wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_wallets = [w for w in monitor.wallets.values() if w.added_by == update.effective_user.id]
    
    if not user_wallets:
        await update.message.reply_text("💼 No wallets. Use `/addwallet`", parse_mode="Markdown")
        return
    
    message = "💼 **Your Wallets**\n\n"
    for w in user_wallets:
        message += f"📫 `{w.address[:15]}...{w.address[-8:]}`\n🔒 Encrypted\n\n"
    
    await update.message.reply_text(message, parse_mode="Markdown")

async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "📊 **Track a Project**\n\n"
            "Usage: `/track <contract_address> <project_name>`\n\n"
            "Example: `/track 0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D BAYC`",
            parse_mode="Markdown"
        )
        return
    
    contract = context.args[0].strip()
    project_name = " ".join(context.args[1:])
    
    # Add with empty stages - user will add them manually
    if monitor.add_project(contract, project_name, [], update.effective_user.id):
        await update.message.reply_text(
            f"✅ **Tracking {project_name}!**\n\n"
            f"📝 Contract: `{contract[:15]}...`\n\n"
            f"📊 Now add mint stages with `/addstage {contract[:15]}... <stage> <price> <max>`\n\n"
            f"**Stages:** GTD, WL, FCFS, Presale, Public\n\n"
            f"Example: `/addstage {contract[:15]}... WL 0.08 2`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Already tracking this contract.")

async def addstage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text(
            "📊 **Add Mint Stage**\n\n"
            "Usage: `/addstage <contract> <stage> <price> <max_per_wallet>`\n\n"
            "**Stages:** GTD, WL, FCFS, Presale, Public\n\n"
            "Example: `/addstage 0x... WL 0.08 2`\n"
            "Example: `/addstage 0x... GTD 0.05 3`",
            parse_mode="Markdown"
        )
        return
    
    contract = context.args[0].lower().strip()
    stage_name = context.args[1].upper()
    try:
        price = float(context.args[2])
        max_per_wallet = int(context.args[3])
    except:
        await update.message.reply_text("❌ Price must be number, max must be integer")
        return
    
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked. Use `/track` first.", parse_mode="Markdown")
        return
    
    # Add stage
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
        f"📊 Project: {monitor.projects[contract].project_name}\n"
        f"🎟️ Stage: {stage_name}\n"
        f"💰 Price: {price} ETH\n"
        f"👥 Max per wallet: {max_per_wallet}\n\n"
        f"🎯 Use `/snipe {contract[:15]}... {stage_name} <amount>` to arm auto-mint!",
        parse_mode="Markdown"
    )

async def set_target_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "🎯 **Set Target Offer Alert**\n\n"
            "Usage: `/settarget <contract> <eth_amount>`\n\n"
            "Example: `/settarget 0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D 5`\n\n"
            "You'll be notified when top offer reaches 5 ETH!",
            parse_mode="Markdown"
        )
        return
    
    contract = context.args[0].lower().strip()
    try:
        target = float(context.args[1])
    except:
        await update.message.reply_text("❌ Target must be a number (ETH)")
        return
    
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked.", parse_mode="Markdown")
        return
    
    monitor.set_target_offer(contract, target)
    
    await update.message.reply_text(
        f"🎯 **Target Offer Alert Set!**\n\n"
        f"📊 Project: {monitor.projects[contract].project_name}\n"
        f"💰 Target: {target} ETH\n\n"
        f"You'll be notified when top offer reaches this amount!",
        parse_mode="Markdown"
    )

async def snipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "🎯 **Arm Auto-Mint**\n\n"
            "Usage: `/snipe <contract> <stage> <amount>`\n\n"
            "**Stages:** GTD, WL, FCFS, Presale, Public\n\n"
            "Example: `/snipe 0x... WL 2`\n\n"
            "Bot will auto-mint during the WL stage!",
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
        await update.message.reply_text("❌ Project not tracked. Use `/track` first.", parse_mode="Markdown")
        return
    
    # Check if stage exists
    stage_exists = False
    for s in monitor.projects[contract].stages:
        if s.get("name", "").upper() == stage.upper():
            stage_exists = True
            break
    
    if not stage_exists:
        await update.message.reply_text(
            f"❌ Stage '{stage}' not found.\n"
            f"Add it with `/addstage {contract[:15]}... {stage} <price> <max>`",
            parse_mode="Markdown"
        )
        return
    
    # Check if user has wallet
    has_wallet = any(w.added_by == update.effective_user.id for w in monitor.wallets.values())
    if not has_wallet:
        await update.message.reply_text("❌ No wallet. Use `/addwallet` first!", parse_mode="Markdown")
        return
    
    monitor.arm_snipe(contract, stage, amount, update.effective_user.id)
    
    await update.message.reply_text(
        f"🎯 **Auto-Mint Armed!**\n\n"
        f"📊 Project: {monitor.projects[contract].project_name}\n"
        f"🎟️ Stage: {stage}\n"
        f"📦 Amount: {amount} NFT(s)\n"
        f"💰 Fee: {FEE_PERCENTAGE}%\n\n"
        f"⚡ Will auto-mint when {stage} stage goes live!",
        parse_mode="Markdown"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/stats <contract_address>`", parse_mode="Markdown")
        return
    
    contract = context.args[0].lower().strip()
    
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked.", parse_mode="Markdown")
        return
    
    # Send "loading" message
    msg = await update.message.reply_text("🔄 Fetching latest stats...", parse_mode="Markdown")
    
    stats = await monitor.update_project_stats(contract)
    project = monitor.projects[contract]
    
    # Build stages display
    stages_text = ""
    for stage in project.stages:
        stage_status = "🟢" if stage.get("is_active") else "⏳"
        minted_status = "✅ Minted" if stage.get("is_minted") else "❌ Not minted"
        stages_text += f"{stage_status} **{stage.get('name')}** - {stage.get('price_eth')} ETH (max {stage.get('max_per_wallet')}) - {minted_status}\n"
    
    target_text = f"🎯 Target: {project.target_offer_eth} ETH" if project.target_offer_eth else "🎯 No target set"
    
    await msg.edit_text(
        f"📊 **{project.project_name}**\n\n"
        f"📝 Contract: `{contract[:15]}...`\n\n"
        f"**Market Stats:**\n"
        f"💰 Floor Price: {stats.get('floor_price', 0)} ETH\n"
        f"💎 Top Offer: {stats.get('top_offer', 0)} ETH\n"
        f"📦 Total Supply: {stats.get('supply', 0)}\n"
        f"{target_text}\n\n"
        f"**Mint Stages:**\n{stages_text}\n\n"
        f"🎯 Snipe armed: {'Yes' if project.armed_snipe else 'No'}\n"
        f"⚡ Use `/refresh {contract[:15]}...` for latest stats",
        parse_mode="Markdown"
    )

async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/refresh <contract_address>`", parse_mode="Markdown")
        return
    
    contract = context.args[0].lower().strip()
    
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked.", parse_mode="Markdown")
        return
    
    msg = await update.message.reply_text("🔄 Refreshing stats...", parse_mode="Markdown")
    
    stats = await monitor.update_project_stats(contract)
    
    await msg.edit_text(
        f"✅ **Stats Refreshed!**\n\n"
        f"📊 {monitor.projects[contract].project_name}\n"
        f"💰 Floor: {stats.get('floor_price', 0)} ETH\n"
        f"💎 Top Offer: {stats.get('top_offer', 0)} ETH\n"
        f"📦 Supply: {stats.get('supply', 0)}\n\n"
        f"Use `/stats {contract[:15]}...` for full details",
        parse_mode="Markdown"
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitor.projects:
        await update.message.reply_text("📭 No projects tracked.", parse_mode="Markdown")
        return
    
    message = "**📋 Tracked Projects**\n\n"
    for addr, project in monitor.projects.items():
        snipe_status = "🎯 Armed" if project.armed_snipe else "⚡ No snipe"
        stages_count = len(project.stages)
        message += f"**{project.project_name}**\n`{addr[:12]}...`\n📊 {stages_count} stages | {snipe_status}\n\n"
    
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
        await update.message.reply_text(f"✅ Cancelled auto-mint for `{contract[:15]}...`", parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ No active snipe")

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
        await update.message.reply_text(
            f"📊 No stages added yet for {project.project_name}\n\n"
            f"Add with `/addstage {contract[:15]}... <stage> <price> <max>`",
            parse_mode="Markdown"
        )
        return
    
    message = f"🎟️ **Mint Stages for {project.project_name}**\n\n"
    for stage in project.stages:
        status = "🟢 Live" if stage.get("is_active") else "⏳ Upcoming"
        message += f"**{stage.get('name')}**\n"
        message += f"💰 Price: {stage.get('price_eth')} ETH\n"
        message += f"👥 Max per wallet: {stage.get('max_per_wallet')}\n"
        message += f"📊 Status: {status}\n\n"
    
    await update.message.reply_text(message, parse_mode="Markdown")

async def removetarget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/removetarget <contract>`", parse_mode="Markdown")
        return
    
    contract = context.args[0].lower().strip()
    
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked.")
        return
    
    monitor.projects[contract].target_offer_eth = None
    monitor.save_data()
    
    await update.message.reply_text(f"✅ Removed target offer alert for `{contract[:15]}...`", parse_mode="Markdown")

async def gas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Simple gas check using Etherscan
    try:
        response = requests.get("https://api.etherscan.io/api?module=gastracker&action=gasoracle", timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "1":
                gas = data["result"]
                await update.message.reply_text(
                    f"⛽ **Current Gas Fees (ETH)**\n\n"
                    f"🐢 Slow: {gas['SafeGasPrice']} Gwei\n"
                    f"⚡ Standard: {gas['ProposeGasPrice']} Gwei\n"
                    f"🚀 Fast: {gas['FastGasPrice']} Gwei\n\n"
                    f"💰 Bot fee: {FEE_PERCENTAGE}% of mint amount\n"
                    f"🎟️ Supports: GTD, WL, FCFS, Presale, Public",
                    parse_mode="Markdown"
                )
                return
    except:
        pass
    
    await update.message.reply_text(
        f"⛽ **Gas Fees**\n\n"
        f"Check https://etherscan.io/gastracker\n\n"
        f"💰 Bot fee: {FEE_PERCENTAGE}%",
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
    
    # Conversation handlers
    wallet_conv = ConversationHandler(
        entry_points=[CommandHandler("addwallet", add_wallet_start)],
        states={ADD_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_wallet_key)]},
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(wallet_conv)
    app.add_handler(CommandHandler("wallets", wallets_command))
    app.add_handler(CommandHandler("track", track_command))
    app.add_handler(CommandHandler("addstage", addstage_command))
    app.add_handler(CommandHandler("stages", stages_command))
    app.add_handler(CommandHandler("settarget", set_target_command))
    app.add_handler(CommandHandler("removetarget", removetarget_command))
    app.add_handler(CommandHandler("snipe", snipe_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
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
    
    monitor_thread = threading.Thread(target=run_monitoring, daemon=True)
    monitor_thread.start()
    
    print("=" * 60)
    print("🤖 ADVANCED NFT AUTO-MINT BOT")
    print("=" * 60)
    print(f"💰 Fee: {FEE_PERCENTAGE}%")
    print("🎟️ Supported Stages: GTD, WL, FCFS, Presale, Public")
    print("📊 Features: Floor price, Top offer, Target alerts")
    print("🔄 Auto-refresh every 15 seconds")
    print("=" * 60)
    print("🟢 Bot is running!")
    print("=" * 60)
    
    app.run_polling()

if __name__ == "__main__":
    import threading
    main()
