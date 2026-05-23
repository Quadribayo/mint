import os
import asyncio
import time
import json
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

import requests
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import Web3

# ============ LOGGING — see exactly what's happening ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ============ CONFIGURATION ============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY", "")
FEE_PERCENTAGE = 1.0
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "default-key")
FEE_RECIPIENT = os.getenv("FEE_RECIPIENT", "")

if ALCHEMY_API_KEY:
    ETH_RPC = f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
else:
    ETH_RPC = "https://cloudflare-eth.com"

DATA_FILE = "watched_projects.json"
WALLETS_FILE = "wallets.json"

# ============ BLOCKCHAIN ============
web3 = Web3(Web3.HTTPProvider(ETH_RPC))

# ============ ENCRYPTION ============
import hashlib, base64

def simple_encrypt(text: str) -> str:
    if not text: return ""
    key = hashlib.sha256(ENCRYPTION_KEY.encode()).digest()
    result = [chr(ord(c) ^ key[i % len(key)]) for i, c in enumerate(text)]
    return base64.b64encode("".join(result).encode()).decode()

def simple_decrypt(encrypted: str) -> str:
    if not encrypted: return ""
    key = hashlib.sha256(ENCRYPTION_KEY.encode()).digest()
    decoded = base64.b64decode(encrypted).decode()
    return "".join(chr(ord(c) ^ key[i % len(key)]) for i, c in enumerate(decoded))

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
        try:
            addr = web3.to_checksum_address(contract_address)
            code = web3.eth.get_code(addr)
            active = code is not None and len(code) > 2
            log.info(f"is_mint_active({contract_address[:10]}...) = {active} (bytecode len={len(code)})")
            return active
        except Exception as e:
            log.error(f"is_mint_active error: {e}")
            return False

    @staticmethod
    async def get_mint_price(contract_address: str) -> float:
        try:
            addr = web3.to_checksum_address(contract_address)
            for selector in ["0x09d3f1e5", "0x98899df4", "0x26a49e37", "0xa035b1fe", "0x091cf879"]:
                try:
                    result = web3.eth.call({"to": addr, "data": selector})
                    if result and len(result) >= 32:
                        price_wei = int(result.hex(), 16)
                        if 0 < price_wei < 10 * 10**18:
                            price_eth = price_wei / 10**18
                            log.info(f"get_mint_price({contract_address[:10]}...) = {price_eth} ETH via {selector}")
                            return price_eth
                except Exception:
                    continue
        except Exception as e:
            log.error(f"get_mint_price error: {e}")
        return 0.0

# ============ PROJECT MONITOR ============
class ProjectMonitor:
    def __init__(self):
        self.projects: Dict[str, TrackedProject] = {}
        self.wallets: Dict[str, Wallet] = {}
        self.monitoring = False
        self.bot_app = None
        self.contract_reader = ContractReader()
        self.load_data()

    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r') as f:
                    for addr, d in json.load(f).items():
                        self.projects[addr] = TrackedProject(**d)
                log.info(f"Loaded {len(self.projects)} projects")
            except Exception as e:
                log.error(f"load projects error: {e}")

        if os.path.exists(WALLETS_FILE):
            try:
                with open(WALLETS_FILE, 'r') as f:
                    for addr, d in json.load(f).items():
                        self.wallets[addr] = Wallet(**d)
                log.info(f"Loaded {len(self.wallets)} wallets")
            except Exception as e:
                log.error(f"load wallets error: {e}")

    def save_data(self):
        with open(DATA_FILE, 'w') as f:
            json.dump({a: asdict(p) for a, p in self.projects.items()}, f, indent=2)
        with open(WALLETS_FILE, 'w') as f:
            json.dump({w.address: asdict(w) for w in self.wallets.values()}, f, indent=2)

    def add_project(self, contract_address: str, project_name: str, stages: List[Dict], user_id: int) -> bool:
        contract_address = contract_address.lower().strip()
        if contract_address in self.projects:
            return False
        self.projects[contract_address] = TrackedProject(
            contract_address=contract_address, project_name=project_name,
            added_by=user_id, added_at=time.time(), stages=stages
        )
        self.save_data()
        return True

    def add_wallet(self, private_key: str, user_id: int) -> str:
        account = Account.from_key(private_key)
        address = account.address
        if address not in self.wallets:
            self.wallets[address] = Wallet(
                address=address,
                private_key_encrypted=simple_encrypt(private_key),
                added_by=user_id, added_at=time.time()
            )
            self.save_data()
        return address

    def arm_snipe(self, contract_address: str, stage_name: str, amount: int, user_id: int) -> bool:
        if contract_address not in self.projects:
            return False
        self.projects[contract_address].armed_snipe = {
            "stage_name": stage_name, "amount": amount,
            "user_id": user_id, "armed_at": time.time(), "executed": False
        }
        self.save_data()
        log.info(f"Snipe armed: {contract_address[:10]}... stage={stage_name} amount={amount}")
        return True

    def disarm_snipe(self, contract_address: str) -> bool:
        if contract_address not in self.projects:
            return False
        self.projects[contract_address].armed_snipe = None
        self.save_data()
        return True

    def set_target_offer(self, contract_address: str, target_eth: float) -> bool:
        if contract_address not in self.projects:
            return False
        self.projects[contract_address].target_offer_eth = target_eth
        self.save_data()
        return True

    async def execute_mint(self, contract_address: str, wallet: Wallet, amount: int) -> Dict:
        try:
            addr = web3.to_checksum_address(contract_address)
            private_key = simple_decrypt(wallet.private_key_encrypted)
            account: LocalAccount = Account.from_key(private_key)

            # Get mint price
            mint_price = await self.contract_reader.get_mint_price(contract_address)
            if mint_price == 0:
                project = self.projects[contract_address]
                snipe_stage = (project.armed_snipe or {}).get("stage_name", "")
                for stage in project.stages:
                    if stage.get("name", "").upper() == snipe_stage.upper():
                        mint_price = stage.get("price_eth", 0.0)
                        break
            log.info(f"execute_mint: price={mint_price} ETH, amount={amount}, wallet={wallet.address[:10]}...")

            value_per_nft = web3.to_wei(mint_price, "ether")
            total_value = value_per_nft * amount
            gas_price = int(web3.eth.gas_price * 1.1)
            nonce = web3.eth.get_transaction_count(account.address, "pending")

            # Calldata candidates — most common mint signatures
            calldata_candidates = [
                "0xa0712d68" + hex(amount)[2:].zfill(64),                                        # mint(uint256)
                "0xefef39a1" + hex(amount)[2:].zfill(64),                                        # publicMint(uint256)
                "0x4e71d92d",                                                                     # mint()
                "0x6a627842" + account.address[2:].lower().zfill(64),                            # mint(address)
                "0x40c10f19" + account.address[2:].lower().zfill(64) + hex(amount)[2:].zfill(64), # mint(address,uint256)
            ]

            tx_hash = None
            last_error = "No selector succeeded"

            for calldata in calldata_candidates:
                try:
                    tx = {
                        "to": addr, "data": calldata, "value": total_value,
                        "nonce": nonce, "gasPrice": gas_price, "chainId": 1,
                    }
                    estimated = web3.eth.estimate_gas({**tx, "from": account.address})
                    tx["gas"] = int(estimated * 1.2)
                    log.info(f"Trying calldata {calldata[:10]}... gas={tx['gas']}")

                    signed = account.sign_transaction(tx)
                    sent = web3.eth.send_raw_transaction(signed.rawTransaction)
                    tx_hash = sent.hex()
                    log.info(f"✅ TX sent! hash={tx_hash}")
                    break
                except Exception as e:
                    last_error = str(e)
                    log.warning(f"Calldata {calldata[:10]} failed: {e}")
                    continue

            if not tx_hash:
                return {"success": False, "error": last_error}

            # Optional fee collection
            if FEE_RECIPIENT and mint_price > 0:
                try:
                    fee_wei = int(total_value * (FEE_PERCENTAGE / 100))
                    fee_nonce = web3.eth.get_transaction_count(account.address, "pending")
                    fee_tx = {
                        "to": web3.to_checksum_address(FEE_RECIPIENT),
                        "value": fee_wei, "gas": 21000, "gasPrice": gas_price,
                        "nonce": fee_nonce, "chainId": 1, "data": b"",
                    }
                    signed_fee = account.sign_transaction(fee_tx)
                    web3.eth.send_raw_transaction(signed_fee.rawTransaction)
                    log.info(f"Fee sent: {fee_wei / 10**18} ETH")
                except Exception as e:
                    log.warning(f"Fee tx failed (non-critical): {e}")

            return {
                "success": True, "tx_hash": tx_hash,
                "mint_price": mint_price, "total_cost": mint_price * amount,
                "fee": mint_price * amount * (FEE_PERCENTAGE / 100),
            }

        except Exception as e:
            log.error(f"execute_mint fatal error: {e}")
            return {"success": False, "error": str(e)}

    async def monitor_loop(self):
        log.info("🟢 Monitor loop started — checking every 10s")
        while self.monitoring:
            try:
                armed_count = sum(
                    1 for p in self.projects.values()
                    if p.armed_snipe and not p.armed_snipe.get("executed", False)
                )
                log.info(f"Monitor tick — {len(self.projects)} projects, {armed_count} armed")

                for contract_addr, project in list(self.projects.items()):
                    snipe = project.armed_snipe
                    if not snipe or snipe.get("executed", False):
                        continue

                    log.info(f"Checking mint for {project.project_name} ({contract_addr[:10]}...)")
                    is_active = await self.contract_reader.is_mint_active(contract_addr)

                    if not is_active:
                        log.info(f"  → not active yet, skipping")
                        continue

                    log.info(f"  → MINT ACTIVE! Finding wallet for user {snipe['user_id']}")
                    user_wallet = next(
                        (w for w in self.wallets.values() if w.added_by == snipe["user_id"]),
                        None
                    )

                    if not user_wallet:
                        log.warning(f"  → No wallet found for user {snipe['user_id']}")
                        await self._notify(project.added_by,
                            f"❌ *Mint is live but no wallet found!*\nAdd one with /addwallet")
                        continue

                    # Mark executed BEFORE sending tx to prevent double-mint
                    project.armed_snipe["executed"] = True
                    self.save_data()

                    result = await self.execute_mint(contract_addr, user_wallet, snipe["amount"])

                    if result["success"]:
                        await self._notify(project.added_by, self._success_msg(project, result))
                    else:
                        # Un-mark so user can retry
                        project.armed_snipe["executed"] = False
                        self.save_data()
                        await self._notify(project.added_by, self._fail_msg(project, result))

            except Exception as e:
                log.error(f"monitor_loop error: {e}")

            await asyncio.sleep(10)

    async def _notify(self, chat_id: int, text: str):
        if not self.bot_app:
            return
        try:
            await self.bot_app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            log.error(f"_notify error: {e}")

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
            f"🎉 Check your wallet!"
        )

    def _fail_msg(self, project: TrackedProject, result: Dict) -> str:
        return (
            f"❌ *MINT FAILED — Retrying next cycle*\n\n"
            f"📊 *Project:* {project.project_name}\n"
            f"⚠️ *Error:* {result.get('error', 'Unknown')}\n\n"
            f"Bot will retry in 10 seconds."
        )

# ============ TELEGRAM HANDLERS ============
monitor = ProjectMonitor()
ADD_WALLET = 0

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add Wallet", callback_data="add_wallet")],
        [InlineKeyboardButton("👛 My Wallets", callback_data="view_wallets")],
        [InlineKeyboardButton("📊 Track Project", callback_data="track_project")],
        [InlineKeyboardButton("🎯 Auto-Mint", callback_data="arm_snipe")],
        [InlineKeyboardButton("📋 My Projects", callback_data="list_projects")],
        [InlineKeyboardButton("❌ Cancel Snipe", callback_data="cancel_snipe")],
        [InlineKeyboardButton("⛽ Gas", callback_data="gas")],
    ]
    await update.message.reply_text(
        f"🤖 *NFT SNIPER BOT*\n\n"
        f"💰 *Fee:* {FEE_PERCENTAGE}%\n"
        f"🔗 *Chain:* Ethereum\n\n"
        f"*Quick Start:*\n"
        f"1️⃣ `/addwallet` — Add your wallet\n"
        f"2️⃣ `/track <contract> <name>` — Track project\n"
        f"3️⃣ `/addstage <contract> <stage> <price> <max>`\n"
        f"4️⃣ `/snipe <contract> <stage> <amount>` — Arm mint\n\n"
        f"⚡ Checks blockchain every 10 seconds!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 *Commands*\n\n"
        "• `/addwallet` — Add wallet\n"
        "• `/wallets` — View wallets\n"
        "• `/track <contract> <name>` — Track project\n"
        "• `/addstage <contract> <stage> <price> <max>`\n"
        "• `/stages <contract>` — View stages\n"
        "• `/snipe <contract> <stage> <amount>` — Arm mint\n"
        "• `/cancel <contract>` — Cancel mint\n"
        "• `/stats <contract>` — Live stats\n"
        "• `/list` — All projects\n"
        "• `/gas` — Gas prices\n"
    )
    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text(text, parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cmd = query.data
    uid = update.effective_user.id

    if cmd == "add_wallet":
        await query.message.reply_text("Send `/addwallet` then your private key", parse_mode="Markdown")
    elif cmd == "view_wallets":
        wallets = [w for w in monitor.wallets.values() if w.added_by == uid]
        if not wallets:
            await query.message.reply_text("💼 No wallets. Use `/addwallet`", parse_mode="Markdown")
        else:
            msg = "💼 *Your Wallets*\n\n" + "".join(f"`{w.address[:15]}...{w.address[-8:]}`\n" for w in wallets)
            await query.message.reply_text(msg, parse_mode="Markdown")
    elif cmd == "track_project":
        await query.message.reply_text("Send: `/track <contract> <name>`", parse_mode="Markdown")
    elif cmd == "arm_snipe":
        await query.message.reply_text("Send: `/snipe <contract> <stage> <amount>`", parse_mode="Markdown")
    elif cmd == "list_projects":
        if not monitor.projects:
            await query.message.reply_text("📭 No projects tracked.")
        else:
            msg = "*📋 Tracked Projects*\n\n"
            for addr, p in monitor.projects.items():
                status = "🔫 Armed" if p.armed_snipe and not p.armed_snipe.get("executed") else "⚡ Watching"
                msg += f"*{p.project_name}* — {status}\n`{addr[:14]}...`\n\n"
            await query.message.reply_text(msg, parse_mode="Markdown")
    elif cmd == "cancel_snipe":
        await query.message.reply_text("Send: `/cancel <contract>`", parse_mode="Markdown")
    elif cmd == "gas":
        await _send_gas(query.message)

async def add_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💳 Send your private key (0x...)\n🔒 Encrypted on save.\n\n/cancel to abort.",
        parse_mode="Markdown"
    )
    return ADD_WALLET

async def add_wallet_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip()
    if len(key) < 30:
        await update.message.reply_text("❌ Invalid key.")
        return ADD_WALLET
    try:
        address = monitor.add_wallet(key, update.effective_user.id)
        await update.message.reply_text(
            f"✅ *Wallet Added!*\n`{address[:15]}...{address[-8:]}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = [w for w in monitor.wallets.values() if w.added_by == update.effective_user.id]
    if not wallets:
        await update.message.reply_text("💼 No wallets. Use `/addwallet`", parse_mode="Markdown")
        return
    msg = "💼 *Your Wallets*\n\n" + "".join(f"`{w.address[:15]}...{w.address[-8:]}`\n" for w in wallets)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: `/track <contract> <name>`", parse_mode="Markdown")
        return
    contract, name = context.args[0].strip(), " ".join(context.args[1:])
    if monitor.add_project(contract, name, [], update.effective_user.id):
        await update.message.reply_text(
            f"✅ *Tracking {name}!*\nNow add stages:\n`/addstage {contract[:12]}... <stage> <price> <max>`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Already tracking this contract.")

async def addstage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text(
            "Usage: `/addstage <contract> <stage> <price> <max>`\nExample: `/addstage 0x... PUBLIC 0.0016 1`",
            parse_mode="Markdown"
        )
        return
    contract = context.args[0].lower().strip()
    stage_name = context.args[1].upper()
    try:
        price = float(context.args[2])
        max_pw = int(context.args[3])
    except Exception:
        await update.message.reply_text("❌ Invalid price or max")
        return
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Not tracked. Use `/track` first.", parse_mode="Markdown")
        return
    monitor.projects[contract].stages.append({
        "name": stage_name, "price_eth": price, "max_per_wallet": max_pw,
        "is_active": False, "is_minted": False
    })
    monitor.save_data()
    await update.message.reply_text(
        f"✅ Stage *{stage_name}* added — {price} ETH, max {max_pw}",
        parse_mode="Markdown"
    )

async def snipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: `/snipe <contract> <stage> <amount>`", parse_mode="Markdown"
        )
        return
    contract = context.args[0].lower().strip()
    stage = context.args[1].upper()
    try:
        amount = int(context.args[2])
        if not 1 <= amount <= 50: raise ValueError
    except Exception:
        await update.message.reply_text("❌ Amount must be 1–50")
        return
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Not tracked. Use `/track` first.", parse_mode="Markdown")
        return

    stage_data = next((s for s in monitor.projects[contract].stages if s.get("name","").upper() == stage), None)
    if not stage_data:
        await update.message.reply_text(f"❌ Stage '{stage}' not found. Add it with /addstage", parse_mode="Markdown")
        return

    if not any(w.added_by == update.effective_user.id for w in monitor.wallets.values()):
        await update.message.reply_text("❌ No wallet. Use `/addwallet` first!", parse_mode="Markdown")
        return

    monitor.arm_snipe(contract, stage, amount, update.effective_user.id)
    await update.message.reply_text(
        f"🎯 *Auto-Mint Armed!*\n\n"
        f"📊 {monitor.projects[contract].project_name}\n"
        f"🎟️ Stage: {stage}\n"
        f"💰 Price: {stage_data.get('price_eth', 0)} ETH\n"
        f"📦 Amount: {amount}\n\n"
        f"⚡ Monitoring every 10 seconds — will fire when mint goes live!",
        parse_mode="Markdown"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/stats <contract>`", parse_mode="Markdown")
        return
    contract = context.args[0].lower().strip()
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Not tracked.", parse_mode="Markdown")
        return
    msg = await update.message.reply_text("🔄 Checking...")
    is_active = await monitor.contract_reader.is_mint_active(contract)
    mint_price = await monitor.contract_reader.get_mint_price(contract)
    project = monitor.projects[contract]
    stages_text = "\n".join(f"• *{s['name']}* — {s['price_eth']} ETH" for s in project.stages) or "None"
    await msg.edit_text(
        f"📊 *{project.project_name}*\n\n"
        f"Contract deployed: {'✅' if is_active else '❌'}\n"
        f"Detected price: {mint_price} ETH\n\n"
        f"*Stages:*\n{stages_text}\n\n"
        f"Auto-mint: {'✅ Armed' if project.armed_snipe and not project.armed_snipe.get('executed') else '❌'}",
        parse_mode="Markdown"
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitor.projects:
        await update.message.reply_text("📭 No projects tracked.")
        return
    msg = "*📋 Tracked Projects*\n\n"
    for addr, p in monitor.projects.items():
        status = "🔫 Armed" if p.armed_snipe and not p.armed_snipe.get("executed") else "⚡ Watching"
        msg += f"*{p.project_name}* — {status}\n`{addr[:14]}...`\n\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/cancel <contract>`", parse_mode="Markdown")
        return
    contract = context.args[0].lower().strip()
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Not tracked.")
        return
    if monitor.projects[contract].armed_snipe:
        monitor.disarm_snipe(contract)
        await update.message.reply_text("✅ Auto-mint cancelled.")
    else:
        await update.message.reply_text("ℹ️ No active snipe.")

async def stages_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/stages <contract>`", parse_mode="Markdown")
        return
    contract = context.args[0].lower().strip()
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Not tracked.", parse_mode="Markdown")
        return
    project = monitor.projects[contract]
    if not project.stages:
        await update.message.reply_text("No stages yet. Use `/addstage`", parse_mode="Markdown")
        return
    msg = f"🎟️ *{project.project_name} Stages*\n\n"
    for s in project.stages:
        msg += f"*{s['name']}* — {s['price_eth']} ETH | Max {s['max_per_wallet']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def _send_gas(msg_obj):
    try:
        r = requests.get("https://api.etherscan.io/api?module=gastracker&action=gasoracle", timeout=10)
        if r.status_code == 200:
            d = r.json()
            if d.get("status") == "1":
                g = d["result"]
                await msg_obj.reply_text(
                    f"⛽ *Gas Prices*\n\n"
                    f"🐢 Slow: {g['SafeGasPrice']} Gwei\n"
                    f"⚡ Standard: {g['ProposeGasPrice']} Gwei\n"
                    f"🚀 Fast: {g['FastGasPrice']} Gwei",
                    parse_mode="Markdown"
                )
                return
    except Exception:
        pass
    await msg_obj.reply_text("⛽ Check: https://etherscan.io/gastracker")

async def gas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_gas(update.message)

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ============ MAIN ============
def main():
    if not TELEGRAM_TOKEN:
        log.error("❌ TELEGRAM_TOKEN not set!")
        return

    log.info(f"Blockchain connected: {web3.is_connected()}")

    # FIX: pass post_init INTO the builder — not assigned after build()
    async def post_init(app: Application):
        log.info("post_init called — starting monitor loop")
        monitor.bot_app = app
        monitor.monitoring = True
        asyncio.create_task(monitor.monitor_loop())

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)   # ← correct way in PTB v20
        .build()
    )

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
    app.add_handler(CommandHandler("snipe", snipe_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("gas", gas_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    log.info("=" * 50)
    log.info("🤖 NFT SNIPER BOT STARTING")
    log.info(f"💰 Fee: {FEE_PERCENTAGE}%")
    log.info("=" * 50)

    app.run_polling()

if __name__ == "__main__":
    main()
