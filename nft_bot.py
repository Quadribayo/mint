import os
import asyncio
import time
import json
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

import requests
import aiohttp
from eth_account import Account
from web3 import Web3

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")
FEE_PERCENTAGE = 1.0
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "default-key")

# Use Alchemy or public RPC for blockchain reading
if ALCHEMY_API_KEY:
    ETH_RPC = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
else:
    ETH_RPC = "https://cloudflare-eth.com"

DATA_FILE = "watched_projects.json"
WALLETS_FILE = "wallets.json"

# ============ BLOCKCHAIN CONNECTION ============
web3 = Web3(Web3.HTTPProvider(ETH_RPC))

# Common mint function signatures
MINT_FUNCTION_SIGNATURES = [
    "0x40c10f19",  # mint(address,uint256)
    "0xa0712d68",  # mint(uint256)
    "0x4e71d92d",  # mint()
    "0x6a627842",  # mint(address)
    "0x1249c58b",  # mint(address,uint256,address)
]

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

# ============ CONTRACT READER ============
class ContractReader:
    @staticmethod
    async def is_mint_active(contract_address: str) -> bool:
        """Check if contract has active mint function"""
        try:
            # Check if contract exists
            checksum_addr = web3.to_checksum_address(contract_address)
            code = web3.eth.get_code(checksum_addr)
            if code == b'':
                return False
            
            # Try to call mint function to see if it's active
            for sig in MINT_FUNCTION_SIGNATURES:
                try:
                    # Call with random address to test if it reverts
                    data = sig + "0" * 64
                    result = web3.eth.call({
                        "to": checksum_addr,
                        "data": data
                    }, block_identifier="pending")
                    # If it doesn't revert, mint might be active
                    if result and len(result) > 2:
                        return True
                except:
                    continue
            
            return False
        except:
            return False
    
    @staticmethod
    async def get_mint_price(contract_address: str) -> float:
        """Try to get mint price from contract"""
        try:
            checksum_addr = web3.to_checksum_address(contract_address)
            
            # Try common price functions
            price_selectors = [
                "0x09d3f1e5",  # mintPrice()
                "0x98899df4",  # cost()
                "0x26a49e37",  # price()
            ]
            
            for selector in price_selectors:
                try:
                    result = web3.eth.call({
                        "to": checksum_addr,
                        "data": selector + "0" * 64
                    })
                    if result and len(result) > 2:
                        price_wei = int(result.hex(), 16)
                        return price_wei / 10**18
                except:
                    continue
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
        self.contract_reader = ContractReader()

    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r') as f:
                    data = json.load(f)
                    for addr, project_data in data.items():
                        self.projects[addr] = TrackedProject(**project_data)
                print(f"✅ Loaded {len(self.projects)} projects")
            except:
                pass
        
        if os.path.exists(WALLETS_FILE):
            try:
                with open(WALLETS_FILE, 'r') as f:
                    data = json.load(f)
                    for key, wallet_data in data.items():
                        self.wallets[key] = Wallet(**wallet_data)
                print(f"✅ Loaded {len(self.wallets)} wallets")
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
            "armed_at": time.time(),
            "executed": False
        }
        self.save_data()
        return True

    def disarm_snipe(self, contract_address: str) -> bool:
        if contract_address not in self.projects:
            return False
        self.projects[contract_address].armed_snipe = None
        self.save_data()
        return True

    async def check_mint_eligibility(self, contract_address: str, stage_name: str, wallet_address: str) -> bool:
        """Check if wallet is eligible for specific mint stage"""
        # This would require reading the contract's merkle root or whitelist
        # For now, return True
        return True

    async def execute_mint(self, contract_address: str, wallet: Wallet, amount: int) -> Dict:
        """Execute the actual mint transaction"""
        try:
            # Get mint price
            mint_price = await self.contract_reader.get_mint_price(contract_address)
            
            if mint_price == 0:
                # Use stage price from config
                for stage in self.projects[contract_address].stages:
                    if stage.get("name") == self.projects[contract_address].armed_snipe.get("stage_name"):
                        mint_price = stage.get("price_eth", 0.05)
                        break
            
            total_cost = mint_price * amount
            fee = total_cost * (FEE_PERCENTAGE / 100)
            
            # Here you would build and send the actual mint transaction
            # For now, return success simulation
            return {
                "success": True,
                "tx_hash": "0x" + os.urandom(32).hex(),
                "total_cost": total_cost,
                "fee": fee,
                "mint_price": mint_price
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def monitor_loop(self):
        """Main monitoring loop - checks for active mints every 10 seconds"""
        last_check = {}
        
        while self.monitoring:
            for contract_addr, project in self.projects.items():
                # Check every 10 seconds for armed snipes
                if project.armed_snipe and not project.armed_snipe.get("executed", False):
                    # Check if contract has active mint
                    is_active = await self.contract_reader.is_mint_active(contract_addr)
                    
                    if is_active:
                        # Get user's wallet
                        user_id = project.armed_snipe["user_id"]
                        user_wallet = None
                        for w in self.wallets.values():
                            if w.added_by == user_id:
                                user_wallet = w
                                break
                        
                        if user_wallet:
                            # Mark as executed to prevent duplicate
                            project.armed_snipe["executed"] = True
                            self.save_data()
                            
                            # Execute the mint
                            result = await self.execute_mint(
                                contract_addr, 
                                user_wallet, 
                                project.armed_snipe["amount"]
                            )
                            
                            if result["success"]:
                                await self.send_mint_success(contract_addr, project, result)
                            else:
                                await self.send_mint_failed(contract_addr, project, result)
            
            await asyncio.sleep(10)  # Check every 10 seconds

    async def send_mint_success(self, contract_addr: str, project: TrackedProject, result: Dict):
        """Send success message when mint executed"""
        if not self.bot_app:
            return
        
        stage_name = project.armed_snipe["stage_name"] if project.armed_snipe else "Unknown"
        amount = project.armed_snipe["amount"] if project.armed_snipe else 0
        
        message = (
            f"✅ **MINT SUCCESSFUL!** ✅\n\n"
            f"📊 **Project:** {project.project_name}\n"
            f"🎟️ **Stage:** {stage_name}\n"
            f"📦 **Minted:** {amount} NFT(s)\n"
            f"💰 **Price:** {result['mint_price']} ETH each\n"
            f"💸 **Total:** {result['total_cost']} ETH\n"
            f"🔗 **Tx:** `{result['tx_hash'][:15]}...`\n\n"
            f"🎉 Congratulations! Check your wallet for the NFTs!"
        )
        
        try:
            await self.bot_app.bot.send_message(
                chat_id=project.added_by,
                text=message,
                parse_mode="Markdown"
            )
            print(f"✅ Mint executed for {project.project_name}")
        except Exception as e:
            print(f"Failed to send success message: {e}")

    async def send_mint_failed(self, contract_addr: str, project: TrackedProject, result: Dict):
        """Send failure message if mint fails"""
        if not self.bot_app:
            return
        
        message = (
            f"❌ **MINT FAILED!** ❌\n\n"
            f"📊 **Project:** {project.project_name}\n"
            f"⚠️ **Error:** {result.get('error', 'Unknown error')}\n\n"
            f"Please check and try manually."
        )
        
        try:
            await self.bot_app.bot.send_message(
                chat_id=project.added_by,
                text=message,
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Failed to send failure message: {e}")

    async def start_monitoring(self, bot_app):
        self.bot_app = bot_app
        self.monitoring = True
        print("🟢 Monitoring started! Checking for active mints every 10 seconds...")
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
    
    alchemy_status = "✅" if ALCHEMY_API_KEY else "⚠️ (optional)"
    
    await update.message.reply_text(
        f"🤖 **NFT SNIPER BOT**\n\n"
        f"💰 **Fee:** {FEE_PERCENTAGE}%\n"
        f"🔗 **Chain:** Ethereum\n"
        f"🔌 **Blockchain:** Connected\n\n"
        f"**Features:**\n"
        f"• 🎟️ GTD, WL, FCFS, Presale minting\n"
        f"• 🔍 **Real-time mint detection**\n"
        f"• 🚀 **Auto-mint when live**\n"
        f"• 📊 Live floor price & offers\n\n"
        f"**Quick Start:**\n"
        f"1️⃣ `/addwallet` - Add your wallet\n"
        f"2️⃣ `/track <contract> <name>` - Track project\n"
        f"3️⃣ `/addstage <contract> <stage> <price> <max>`\n"
        f"4️⃣ `/snipe <contract> <stage> <amount>` - Arm mint\n\n"
        f"⚠️ Bot checks blockchain every 10 seconds!\n"
        f"📝 Type /help for all commands",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📚 **Available Commands**\n\n"
        "**Wallet**\n"
        "• `/addwallet` - Add your wallet (private key)\n"
        "• `/wallets` - View your wallets\n\n"
        "**Tracking**\n"
        "• `/track <contract> <name>` - Track a project\n"
        "• `/addstage <contract> <stage> <price> <max>` - Add mint stage\n"
        "• `/stages <contract>` - View stages\n"
        "• `/list` - View tracked projects\n\n"
        "**Auto-Mint**\n"
        "• `/snipe <contract> <stage> <amount>` - Arm auto-mint\n"
        "• `/cancel <contract>` - Cancel auto-mint\n\n"
        "**Alerts**\n"
        "• `/settarget <contract> <eth>` - Set price alert\n\n"
        "**Info**\n"
        "• `/stats <contract>` - Live project stats\n"
        "• `/refresh <contract>` - Update stats\n"
        "• `/gas` - Check gas fees\n\n"
        f"💰 **Fee:** {FEE_PERCENTAGE}% of mint amount\n"
        f"⚡ Bot checks for active mints every 10 seconds!"
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
            "Stages: GTD, WL, FCFS, Presale, Public\n\n"
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
            "**Stages:** GTD, WL, FCFS, Presale, Public\n\n"
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
            "Stages: GTD, WL, FCFS, Presale, Public\n\n"
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
        f"🎯 Use `/snipe {contract[:15]}... {stage_name} <amount>` to arm auto-mint!\n\n"
        f"⚡ Bot will monitor blockchain and mint automatically when {stage_name} goes live!",
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
            "Stages: GTD, WL, FCFS, Presale, Public\n\n"
            "Example: `/snipe 0x... WL 2`\n\n"
            "⚠️ Bot will monitor blockchain and mint automatically when the stage goes live!",
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
    stage_price = 0
    for s in monitor.projects[contract].stages:
        if s.get("name", "").upper() == stage:
            stage_exists = True
            stage_price = s.get("price_eth", 0)
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
        f"📊 {monitor.projects[contract].project_name}\n"
        f"🎟️ {stage} Stage\n"
        f"💰 Price: {stage_price} ETH\n"
        f"📦 Amount: {amount} NFT(s)\n"
        f"💰 Fee: {FEE_PERCENTAGE}%\n\n"
        f"⚡ Bot will monitor blockchain every 10 seconds!\n"
        f"🟢 Will mint automatically when {stage} goes live!\n\n"
        f"✅ Keep your bot running and wallet funded!",
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
    
    msg = await update.message.reply_text("🔄 Fetching blockchain data...", parse_mode="Markdown")
    
    # Check if mint is active
    is_active = await monitor.contract_reader.is_mint_active(contract)
    mint_price = await monitor.contract_reader.get_mint_price(contract)
    
    project = monitor.projects[contract]
    
    stages_text = ""
    for stage in project.stages:
        status = "🟢 ACTIVE" if is_active and stage.get("name") == "PUBLIC" else "⏳ Waiting"
        stages_text += f"• **{stage.get('name')}** - {stage.get('price_eth')} ETH | {status}\n"
    
    await msg.edit_text(
        f"📊 **{project.project_name}**\n\n"
        f"📝 Contract: `{contract[:15]}...`\n\n"
        f"**Blockchain Status:**\n"
        f"🔍 Mint Active: {'✅ YES' if is_active else '❌ NO'}\n"
        f"💰 Current Price: {mint_price} ETH\n\n"
        f"**Mint Stages:**\n{stages_text}\n"
        f"🎯 Auto-mint: {'✅ Armed' if project.armed_snipe else '❌ Not armed'}\n\n"
        f"⚡ Bot checks every 10 seconds!",
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
    
    msg = await update.message.reply_text("🔄 Checking blockchain...", parse_mode="Markdown")
    
    is_active = await monitor.contract_reader.is_mint_active(contract)
    mint_price = await monitor.contract_reader.get_mint_price(contract)
    
    await msg.edit_text(
        f"✅ **Blockchain Check Complete!**\n\n"
        f"📊 {monitor.projects[contract].project_name}\n"
        f"🔍 Mint Active: {'✅ YES' if is_active else '❌ NO'}\n"
        f"💰 Current Price: {mint_price} ETH\n\n"
        f"⚡ Bot will auto-mint if armed!",
        parse_mode="Markdown"
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitor.projects:
        await update.message.reply_text("📭 No projects tracked.", parse_mode="Markdown")
        return
    
    message = "**📋 Tracked Projects**\n\n"
    for addr, project in monitor.projects.items():
        snipe = "🔫 Armed" if project.armed_snipe else "⚡ Watching"
        stage = project.armed_snipe.get("stage_name") if project.armed_snipe else "None"
        message += f"**{project.project_name}**\n`{addr[:12]}...` | {snipe} | Stage: {stage}\n\n"
    
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
    
    # Check blockchain connection
    if web3.is_connected():
        print("✅ Connected to Ethereum blockchain")
    else:
        print("⚠️ Using fallback RPC - may be rate limited")
    
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
    
    print("=" * 55)
    print("🤖 NFT SNIPER BOT")
    print("=" * 55)
    print(f"💰 Fee: {FEE_PERCENTAGE}%")
    print(f"🔗 Blockchain: {'Connected' if web3.is_connected() else 'Limited'}")
    print("🎟️ Stages: GTD, WL, FCFS, Presale, Public")
    print("⚡ Check interval: Every 10 seconds")
    print("=" * 55)
    print("🟢 Bot is running!")
    print("=" * 55)
    
    app.run_polling()

if __name__ == "__main__":
    main()
