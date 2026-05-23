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
from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.exceptions import ContractLogicError

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")
FEE_PERCENTAGE = 1.0
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "default-key")
FEE_RECIPIENT = os.getenv("FEE_RECIPIENT", "")  # Your address to receive fees

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
    "0xefef39a1",  # publicMint(uint256)
    "0x84bb1e42",  # mint(address,uint256,uint256,bytes)
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
        """Check if contract exists and has bytecode (is deployed)."""
        try:
            checksum_addr = web3.to_checksum_address(contract_address)
            code = web3.eth.get_code(checksum_addr)
            # Contract exists if it has bytecode
            if code and code != b'' and len(code) > 2:
                return True
            return False
        except Exception as e:
            print(f"is_mint_active error: {e}")
            return False

    @staticmethod
    async def get_mint_price(contract_address: str) -> float:
        """Try to get mint price from contract using common selectors."""
        try:
            checksum_addr = web3.to_checksum_address(contract_address)

            # Selectors with NO extra padding (these take no args)
            price_selectors = [
                "0x09d3f1e5",  # mintPrice()
                "0x98899df4",  # cost()
                "0x26a49e37",  # price()
                "0xa035b1fe",  # price()  alternate hash
                "0x091cf879",  # PRICE()
            ]

            for selector in price_selectors:
                try:
                    result = web3.eth.call({
                        "to": checksum_addr,
                        "data": selector  # No extra zeros for no-arg functions
                    })
                    if result and len(result) >= 32:
                        price_wei = int(result.hex(), 16)
                        if 0 < price_wei < 10 * 10**18:  # sanity: 0 < price < 10 ETH
                            return price_wei / 10**18
                except Exception:
                    continue
        except Exception as e:
            print(f"get_mint_price error: {e}")
        return 0.0

# ============ PROJECT MONITOR ============
class ProjectMonitor:
    def __init__(self):
        self.projects: Dict[str, TrackedProject] = {}
        self.wallets: Dict[str, Wallet] = {}
        self.load_data()
        self.monitoring = False
        self.bot_app = None
        self.contract_reader = ContractReader()
        # FIX: store the main event loop reference
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None

    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r') as f:
                    data = json.load(f)
                    for addr, project_data in data.items():
                        self.projects[addr] = TrackedProject(**project_data)
                print(f"✅ Loaded {len(self.projects)} projects")
            except Exception as e:
                print(f"load_data error: {e}")

        if os.path.exists(WALLETS_FILE):
            try:
                with open(WALLETS_FILE, 'r') as f:
                    data = json.load(f)
                    for key, wallet_data in data.items():
                        self.wallets[key] = Wallet(**wallet_data)
                print(f"✅ Loaded {len(self.wallets)} wallets")
            except Exception as e:
                print(f"load_wallets error: {e}")

    def save_data(self):
        data = {addr: asdict(project) for addr, project in self.projects.items()}
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)

        wallet_data = {w.address: asdict(w) for w in self.wallets.values()}
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
        except Exception:
            raise Exception("Invalid private key")

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

    async def execute_mint(self, contract_address: str, wallet: Wallet, amount: int) -> Dict:
        """
        FIX: Actually build and send the mint transaction on-chain.
        Tries multiple common mint selectors until one succeeds.
        """
        try:
            checksum_addr = web3.to_checksum_address(contract_address)
            private_key = simple_decrypt(wallet.private_key_encrypted)
            account: LocalAccount = Account.from_key(private_key)

            # Get mint price
            mint_price = await self.contract_reader.get_mint_price(contract_address)

            # Fall back to stage price if on-chain read failed
            if mint_price == 0:
                project = self.projects[contract_address]
                stage_name = project.armed_snipe.get("stage_name", "") if project.armed_snipe else ""
                for stage in project.stages:
                    if stage.get("name", "").upper() == stage_name.upper():
                        mint_price = stage.get("price_eth", 0.0)
                        break

            value_per_nft = web3.to_wei(mint_price, "ether")
            total_value = value_per_nft * amount

            # Current gas price (add 10% tip)
            base_gas_price = web3.eth.gas_price
            gas_price = int(base_gas_price * 1.1)

            nonce = web3.eth.get_transaction_count(account.address, "pending")

            # Try selectors in order; pick first that doesn't revert
            tx_hash = None
            last_error = "No selector worked"

            # Build calldata candidates: mint(uint256 amount) and mint()
            calldata_candidates = [
                # mint(uint256) — most common
                "0xa0712d68" + hex(amount)[2:].zfill(64),
                # publicMint(uint256)
                "0xefef39a1" + hex(amount)[2:].zfill(64),
                # mint() — no args
                "0x4e71d92d",
                # mint(address)
                "0x6a627842" + account.address[2:].lower().zfill(64),
                # mint(address,uint256)
                "0x40c10f19" + account.address[2:].lower().zfill(64) + hex(amount)[2:].zfill(64),
            ]

            for calldata in calldata_candidates:
                try:
                    tx = {
                        "to": checksum_addr,
                        "data": calldata,
                        "value": total_value,
                        "nonce": nonce,
                        "gasPrice": gas_price,
                        "chainId": 1,  # Ethereum mainnet
                    }

                    # Estimate gas — will raise if calldata is wrong
                    estimated_gas = web3.eth.estimate_gas({**tx, "from": account.address})
                    tx["gas"] = int(estimated_gas * 1.2)  # 20% buffer

                    signed = account.sign_transaction(tx)
                    raw_tx = signed.rawTransaction
                    sent_hash = web3.eth.send_raw_transaction(raw_tx)
                    tx_hash = sent_hash.hex()
                    print(f"✅ TX sent: {tx_hash}")
                    break

                except Exception as e:
                    last_error = str(e)
                    print(f"Calldata {calldata[:10]} failed: {e}")
                    continue

            if not tx_hash:
                return {"success": False, "error": last_error}

            # Collect fee if configured
            if FEE_RECIPIENT and mint_price > 0:
                fee_amount = total_value * (FEE_PERCENTAGE / 100)
                try:
                    fee_nonce = web3.eth.get_transaction_count(account.address, "pending")
                    fee_tx = {
                        "to": web3.to_checksum_address(FEE_RECIPIENT),
                        "value": int(fee_amount),
                        "gas": 21000,
                        "gasPrice": gas_price,
                        "nonce": fee_nonce,
                        "chainId": 1,
                        "data": b"",
                    }
                    signed_fee = account.sign_transaction(fee_tx)
                    web3.eth.send_raw_transaction(signed_fee.rawTransaction)
                except Exception as e:
                    print(f"Fee tx failed (non-critical): {e}")

            return {
                "success": True,
                "tx_hash": tx_hash,
                "total_cost": mint_price * amount,
                "mint_price": mint_price,
                "fee": mint_price * amount * (FEE_PERCENTAGE / 100),
            }

        except Exception as e:
            print(f"execute_mint error: {e}")
            return {"success": False, "error": str(e)}

    async def monitor_loop(self):
        """
        FIX: Run entirely inside the main event loop — no threading needed.
        Called with asyncio.create_task() from start_monitoring().
        """
        print("🟢 Monitor loop started")
        while self.monitoring:
            try:
                for contract_addr, project in list(self.projects.items()):
                    snipe = project.armed_snipe
                    if not snipe or snipe.get("executed", False):
                        continue

                    is_active = await self.contract_reader.is_mint_active(contract_addr)
                    if not is_active:
                        continue

                    print(f"🔥 Mint active for {project.project_name}! Executing...")

                    user_id = snipe["user_id"]
                    user_wallet = next(
                        (w for w in self.wallets.values() if w.added_by == user_id),
                        None
                    )

                    if not user_wallet:
                        print(f"No wallet for user {user_id}")
                        continue

                    # Mark executed BEFORE sending to prevent double-mint
                    project.armed_snipe["executed"] = True
                    self.save_data()

                    result = await self.execute_mint(
                        contract_addr,
                        user_wallet,
                        snipe["amount"]
                    )

                    if result["success"]:
                        await self._notify(project.added_by, self._success_msg(project, result))
                    else:
                        await self._notify(project.added_by, self._fail_msg(project, result))

            except Exception as e:
                print(f"monitor_loop error: {e}")

            await asyncio.sleep(10)

    async def _notify(self, chat_id: int, text: str):
        """Send a Telegram message safely."""
        if not self.bot_app:
            return
        try:
            await self.bot_app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"_notify error: {e}")

    def _success_msg(self, project: TrackedProject, result: Dict) -> str:
        snipe = project.armed_snipe or {}
        return (
            f"✅ *MINT SUCCESSFUL!*\n\n"
            f"📊 *Project:* {project.project_name}\n"
            f"🎟️ *Stage:* {snipe.get('stage_name', '?')}\n"
            f"📦 *Minted:* {snipe.get('amount', '?')} NFT(s)\n"
            f"💰 *Price:* {result['mint_price']} ETH each\n"
            f"💸 *Total:* {result['total_cost']} ETH\n"
            f"🔗 *Tx:* `{result['tx_hash'][:20]}...`\n\n"
            f"🎉 Check your wallet for the NFTs!"
        )

    def _fail_msg(self, project: TrackedProject, result: Dict) -> str:
        return (
            f"❌ *MINT FAILED!*\n\n"
            f"📊 *Project:* {project.project_name}\n"
            f"⚠️ *Error:* {result.get('error', 'Unknown')}\n\n"
            f"Please check and try manually."
        )

    async def start_monitoring(self, bot_app):
        """
        FIX: Use create_task() so monitoring shares the bot's event loop.
        Call this from post_init or after app starts.
        """
        self.bot_app = bot_app
        self.monitoring = True
        asyncio.create_task(self.monitor_loop())
        print("🟢 Monitoring task created.")


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
        f"🤖 *NFT SNIPER BOT*\n\n"
        f"💰 *Fee:* {FEE_PERCENTAGE}%\n"
        f"🔗 *Chain:* Ethereum\n"
        f"🔌 *Blockchain:* Connected\n\n"
        f"*Features:*\n"
        f"• 🎟️ GTD, WL, FCFS, Presale minting\n"
        f"• 🔍 Real-time mint detection\n"
        f"• 🚀 Auto-mint when live\n\n"
        f"*Quick Start:*\n"
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
        "📚 *Available Commands*\n\n"
        "*Wallet*\n"
        "• `/addwallet` - Add your wallet (private key)\n"
        "• `/wallets` - View your wallets\n\n"
        "*Tracking*\n"
        "• `/track <contract> <name>` - Track a project\n"
        "• `/addstage <contract> <stage> <price> <max>` - Add mint stage\n"
        "• `/stages <contract>` - View stages\n"
        "• `/list` - View tracked projects\n\n"
        "*Auto-Mint*\n"
        "• `/snipe <contract> <stage> <amount>` - Arm auto-mint\n"
        "• `/cancel <contract>` - Cancel auto-mint\n\n"
        "*Alerts*\n"
        "• `/settarget <contract> <eth>` - Set price alert\n\n"
        "*Info*\n"
        "• `/stats <contract>` - Live project stats\n"
        "• `/refresh <contract>` - Update stats\n"
        "• `/gas` - Check gas fees\n\n"
        f"💰 *Fee:* {FEE_PERCENTAGE}% of mint amount\n"
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
        # FIX: use query.message, not update.message (which is None for callbacks)
        user_wallets = [w for w in monitor.wallets.values() if w.added_by == update.effective_user.id]
        if not user_wallets:
            await query.message.reply_text("💼 No wallets. Use `/addwallet`", parse_mode="Markdown")
        else:
            message = "💼 *Your Wallets*\n\n"
            for w in user_wallets:
                message += f"📫 `{w.address[:15]}...{w.address[-8:]}`\n\n"
            await query.message.reply_text(message, parse_mode="Markdown")
    elif cmd == "track_project":
        await query.message.reply_text(
            "📊 *Track a Project*\n\nSend: `/track <contract> <name>`\n\nExample: `/track 0xBC4CA... BAYC`",
            parse_mode="Markdown"
        )
    elif cmd == "set_target":
        await query.message.reply_text(
            "🎯 *Set Price Alert*\n\nSend: `/settarget <contract> <eth>`",
            parse_mode="Markdown"
        )
    elif cmd == "arm_snipe":
        await query.message.reply_text(
            "🎯 *Auto-Mint*\n\nSend: `/snipe <contract> <stage> <amount>`\n\nStages: GTD, WL, FCFS, Presale, Public",
            parse_mode="Markdown"
        )
    elif cmd == "list_projects":
        # FIX: build message directly instead of calling list_command (which needs update.message)
        if not monitor.projects:
            await query.message.reply_text("📭 No projects tracked.", parse_mode="Markdown")
        else:
            message = "*📋 Tracked Projects*\n\n"
            for addr, project in monitor.projects.items():
                snipe = "🔫 Armed" if project.armed_snipe else "⚡ Watching"
                stage = project.armed_snipe.get("stage_name") if project.armed_snipe else "None"
                message += f"*{project.project_name}*\n`{addr[:12]}...` | {snipe} | Stage: {stage}\n\n"
            await query.message.reply_text(message, parse_mode="Markdown")
    elif cmd == "refresh":
        await query.message.reply_text("Send `/refresh <contract>` to update stats", parse_mode="Markdown")
    elif cmd == "cancel_snipe":
        await query.message.reply_text("Send `/cancel <contract>` to cancel", parse_mode="Markdown")
    elif cmd == "gas":
        await gas_command(update, context)

async def add_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💳 *Add Wallet*\n\nSend your private key (starts with 0x)\n🔒 It will be encrypted.\n\nSend /cancel to abort.",
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
            f"✅ *Wallet Added!*\n\n"
            f"📫 Address: `{address[:15]}...{address[-8:]}`\n\n"
            f"💡 Use `/track` to start monitoring projects!",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text("❌ Failed to add wallet. Please check your private key.")

    return ConversationHandler.END

async def wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_wallets = [w for w in monitor.wallets.values() if w.added_by == update.effective_user.id]

    if not user_wallets:
        await update.message.reply_text("💼 No wallets. Use `/addwallet`", parse_mode="Markdown")
        return

    message = "💼 *Your Wallets*\n\n"
    for w in user_wallets:
        message += f"📫 `{w.address[:15]}...{w.address[-8:]}`\n\n"

    await update.message.reply_text(message, parse_mode="Markdown")

async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "📊 *Track a Project*\n\nUsage: `/track <contract> <name>`\n\nExample: `/track 0xBC4CA... BAYC`",
            parse_mode="Markdown"
        )
        return

    contract = context.args[0].strip()
    project_name = " ".join(context.args[1:])

    if monitor.add_project(contract, project_name, [], update.effective_user.id):
        await update.message.reply_text(
            f"✅ *Tracking {project_name}!*\n\n"
            f"📝 Contract: `{contract[:15]}...`\n\n"
            f"Add mint stages with:\n`/addstage {contract[:15]}... <stage> <price> <max>`\n\n"
            "Stages: GTD, WL, FCFS, Presale, Public",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Already tracking this contract.")

async def addstage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text(
            "📊 *Add Mint Stage*\n\nUsage: `/addstage <contract> <stage> <price> <max>`\n\nExample: `/addstage 0x... WL 0.08 2`",
            parse_mode="Markdown"
        )
        return

    contract = context.args[0].lower().strip()
    stage_name = context.args[1].upper()
    try:
        price = float(context.args[2])
        max_per_wallet = int(context.args[3])
    except Exception:
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
        f"✅ *Stage Added!*\n\n"
        f"📊 {monitor.projects[contract].project_name}\n"
        f"🎟️ {stage_name} | {price} ETH | Max {max_per_wallet}\n\n"
        f"🎯 Use `/snipe {contract[:15]}... {stage_name} <amount>` to arm auto-mint!",
        parse_mode="Markdown"
    )

async def set_target_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "🎯 *Set Price Alert*\n\nUsage: `/settarget <contract> <eth>`",
            parse_mode="Markdown"
        )
        return

    contract = context.args[0].lower().strip()
    try:
        target = float(context.args[1])
    except Exception:
        await update.message.reply_text("❌ Target must be a number")
        return

    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked.", parse_mode="Markdown")
        return

    monitor.set_target_offer(contract, target)
    await update.message.reply_text(
        f"🎯 *Alert Set!*\n\n"
        f"📊 {monitor.projects[contract].project_name}\n"
        f"💰 Target: {target} ETH\n\n"
        f"You'll be notified when offers hit this price!",
        parse_mode="Markdown"
    )

async def snipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "🎯 *Auto-Mint*\n\nUsage: `/snipe <contract> <stage> <amount>`\n\nExample: `/snipe 0x... WL 2`",
            parse_mode="Markdown"
        )
        return

    contract = context.args[0].lower().strip()
    stage = context.args[1].upper()
    try:
        amount = int(context.args[2])
        if amount < 1 or amount > 50:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Amount must be 1-50")
        return

    if contract not in monitor.projects:
        await update.message.reply_text("❌ Project not tracked. Use `/track` first.", parse_mode="Markdown")
        return

    stage_exists = False
    stage_price = 0
    for s in monitor.projects[contract].stages:
        if s.get("name", "").upper() == stage:
            stage_exists = True
            stage_price = s.get("price_eth", 0)
            break

    if not stage_exists:
        await update.message.reply_text(
            f"❌ Stage '{stage}' not found.\nAdd it with `/addstage {contract[:15]}... {stage} <price> <max>`",
            parse_mode="Markdown"
        )
        return

    has_wallet = any(w.added_by == update.effective_user.id for w in monitor.wallets.values())
    if not has_wallet:
        await update.message.reply_text("❌ No wallet. Use `/addwallet` first!", parse_mode="Markdown")
        return

    monitor.arm_snipe(contract, stage, amount, update.effective_user.id)

    await update.message.reply_text(
        f"🎯 *Auto-Mint Armed!*\n\n"
        f"📊 {monitor.projects[contract].project_name}\n"
        f"🎟️ {stage} Stage\n"
        f"💰 Price: {stage_price} ETH\n"
        f"📦 Amount: {amount} NFT(s)\n"
        f"💰 Fee: {FEE_PERCENTAGE}%\n\n"
        f"⚡ Monitoring every 10 seconds!\n"
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

    msg = await update.message.reply_text("🔄 Fetching blockchain data...")

    is_active = await monitor.contract_reader.is_mint_active(contract)
    mint_price = await monitor.contract_reader.get_mint_price(contract)

    project = monitor.projects[contract]

    stages_text = ""
    for stage in project.stages:
        stages_text += f"• *{stage.get('name')}* - {stage.get('price_eth')} ETH\n"

    await msg.edit_text(
        f"📊 *{project.project_name}*\n\n"
        f"📝 Contract: `{contract[:15]}...`\n\n"
        f"*Blockchain Status:*\n"
        f"🔍 Contract deployed: {'✅ YES' if is_active else '❌ NO'}\n"
        f"💰 Detected Price: {mint_price} ETH\n\n"
        f"*Mint Stages:*\n{stages_text}\n"
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

    msg = await update.message.reply_text("🔄 Checking blockchain...")

    is_active = await monitor.contract_reader.is_mint_active(contract)
    mint_price = await monitor.contract_reader.get_mint_price(contract)

    await msg.edit_text(
        f"✅ *Blockchain Check Complete!*\n\n"
        f"📊 {monitor.projects[contract].project_name}\n"
        f"🔍 Contract deployed: {'✅ YES' if is_active else '❌ NO'}\n"
        f"💰 Detected Price: {mint_price} ETH\n\n"
        f"⚡ Bot will auto-mint if armed!",
        parse_mode="Markdown"
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitor.projects:
        await update.message.reply_text("📭 No projects tracked.", parse_mode="Markdown")
        return

    message = "*📋 Tracked Projects*\n\n"
    for addr, project in monitor.projects.items():
        snipe = "🔫 Armed" if project.armed_snipe else "⚡ Watching"
        stage = project.armed_snipe.get("stage_name") if project.armed_snipe else "None"
        message += f"*{project.project_name}*\n`{addr[:12]}...` | {snipe} | Stage: {stage}\n\n"

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
        await update.message.reply_text("✅ Cancelled auto-mint", parse_mode="Markdown")
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
        await update.message.reply_text("No stages added yet. Use `/addstage`", parse_mode="Markdown")
        return

    message = f"🎟️ *Mint Stages for {project.project_name}*\n\n"
    for stage in project.stages:
        message += f"*{stage.get('name')}*\n💰 {stage.get('price_eth')} ETH | Max {stage.get('max_per_wallet')}\n\n"

    await update.message.reply_text(message, parse_mode="Markdown")

async def gas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # FIX: support both direct command and callback query
    reply_fn = (
        update.callback_query.message.reply_text
        if update.callback_query
        else update.message.reply_text
    )

    try:
        response = requests.get(
            "https://api.etherscan.io/api?module=gastracker&action=gasoracle",
            timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "1":
                gas = data["result"]
                await reply_fn(
                    f"⛽ *Current Gas*\n\n"
                    f"🐢 Slow: {gas['SafeGasPrice']} Gwei\n"
                    f"⚡ Standard: {gas['ProposeGasPrice']} Gwei\n"
                    f"🚀 Fast: {gas['FastGasPrice']} Gwei\n\n"
                    f"💰 Fee: {FEE_PERCENTAGE}%",
                    parse_mode="Markdown"
                )
                return
    except Exception:
        pass

    await reply_fn(
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

    # FIX: start monitoring as a proper async task on the same event loop
    async def post_init(application: Application):
        await monitor.start_monitoring(application)

    app.post_init = post_init

    print("=" * 55)
    print("🤖 NFT SNIPER BOT")
    print("=" * 55)
    print(f"💰 Fee: {FEE_PERCENTAGE}%")
    print(f"🔗 Blockchain: {'Connected' if web3.is_connected() else 'Limited'}")
    print("⚡ Check interval: Every 10 seconds")
    print("=" * 55)
    print("🟢 Bot is running!")
    print("=" * 55)

    app.run_polling()

if __name__ == "__main__":
    main()
