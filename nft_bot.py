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

# ============ LOGGING ============
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

ETH_RPC = (
    f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    if ALCHEMY_API_KEY else "https://cloudflare-eth.com"
)

DATA_FILE = "watched_projects.json"
WALLETS_FILE = "wallets.json"

web3 = Web3(Web3.HTTPProvider(ETH_RPC))

# ============ ENCRYPTION ============
import hashlib, base64

def simple_encrypt(text: str) -> str:
    if not text: return ""
    key = hashlib.sha256(ENCRYPTION_KEY.encode()).digest()
    return base64.b64encode("".join(chr(ord(c) ^ key[i % len(key)]) for i, c in enumerate(text)).encode()).decode()

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

# ============ CONVERSATION STATES ============
ADD_WALLET = 0
MANUAL_MINT_CONTRACT = 10
MANUAL_MINT_PRICE     = 11
MANUAL_MINT_AMOUNT    = 12
MANUAL_MINT_WALLET    = 13
REMOVE_WALLET_PICK    = 20
SNIPE_WALLET_PICK     = 30

# ============ CONTRACT READER ============
class ContractReader:
    @staticmethod
    async def is_mint_active(contract_address: str) -> bool:
        try:
            addr = web3.to_checksum_address(contract_address)
            code = web3.eth.get_code(addr)
            active = code is not None and len(code) > 2
            log.info(f"is_mint_active({contract_address[:10]}...) = {active}")
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
                            return price_wei / 10**18
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

    def remove_wallet(self, address: str, user_id: int) -> bool:
        """Remove a wallet — only if it belongs to this user."""
        w = self.wallets.get(address)
        if not w or w.added_by != user_id:
            return False
        del self.wallets[address]
        self.save_data()
        return True

    def get_user_wallets(self, user_id: int) -> List[Wallet]:
        return [w for w in self.wallets.values() if w.added_by == user_id]

    def get_wallet_balance(self, address: str) -> float:
        try:
            return web3.eth.get_balance(web3.to_checksum_address(address)) / 10**18
        except Exception:
            return 0.0

    def arm_snipe(self, contract_address: str, stage_name: str, amount: int,
                  user_id: int, wallet_address: str) -> bool:
        if contract_address not in self.projects:
            return False
        self.projects[contract_address].armed_snipe = {
            "stage_name": stage_name, "amount": amount,
            "user_id": user_id, "wallet_address": wallet_address,
            "armed_at": time.time(), "executed": False, "fail_count": 0
        }
        self.save_data()
        log.info(f"Snipe armed: {contract_address[:10]}... stage={stage_name} wallet={wallet_address[:10]}...")
        return True

    def disarm_snipe(self, contract_address: str) -> bool:
        if contract_address not in self.projects:
            return False
        self.projects[contract_address].armed_snipe = None
        self.save_data()
        return True

    async def execute_mint(self, contract_address: str, wallet: Wallet,
                           amount: int, price_override: float = 0.0) -> Dict:
        try:
            addr = web3.to_checksum_address(contract_address)
            private_key = simple_decrypt(wallet.private_key_encrypted)
            account: LocalAccount = Account.from_key(private_key)

            # Get mint price
            mint_price = price_override or await self.contract_reader.get_mint_price(contract_address)

            # Fall back to stage price if on-chain read failed
            if mint_price == 0 and contract_address in self.projects:
                project = self.projects[contract_address]
                snipe_stage = (project.armed_snipe or {}).get("stage_name", "")
                for stage in project.stages:
                    if stage.get("name", "").upper() == snipe_stage.upper():
                        mint_price = stage.get("price_eth", 0.0)
                        break

            log.info(f"execute_mint: price={mint_price} ETH amount={amount} wallet={wallet.address[:10]}...")

            value_per_nft = web3.to_wei(mint_price, "ether")
            total_value = value_per_nft * amount
            gas_price = int(web3.eth.gas_price * 1.1)
            nonce = web3.eth.get_transaction_count(account.address, "pending")

            calldata_candidates = [
                "0xa0712d68" + hex(amount)[2:].zfill(64),
                "0xefef39a1" + hex(amount)[2:].zfill(64),
                "0x4e71d92d",
                "0x6a627842" + account.address[2:].lower().zfill(64),
                "0x40c10f19" + account.address[2:].lower().zfill(64) + hex(amount)[2:].zfill(64),
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
                    log.info(f"Trying {calldata[:10]}... gas={tx['gas']}")
                    signed = account.sign_transaction(tx)
                    sent = web3.eth.send_raw_transaction(signed.rawTransaction)
                    tx_hash = sent.hex()
                    log.info(f"✅ TX sent! {tx_hash}")
                    break
                except Exception as e:
                    last_error = str(e)
                    log.warning(f"Calldata {calldata[:10]} failed: {e}")
                    continue

            if not tx_hash:
                return {"success": False, "error": last_error}

            # Fee collection
            if FEE_RECIPIENT and mint_price > 0:
                try:
                    fee_wei = int(total_value * (FEE_PERCENTAGE / 100))
                    fee_nonce = web3.eth.get_transaction_count(account.address, "pending")
                    fee_tx = {
                        "to": web3.to_checksum_address(FEE_RECIPIENT),
                        "value": fee_wei, "gas": 21000, "gasPrice": gas_price,
                        "nonce": fee_nonce, "chainId": 1, "data": b"",
                    }
                    web3.eth.send_raw_transaction(account.sign_transaction(fee_tx).rawTransaction)
                except Exception as e:
                    log.warning(f"Fee tx failed: {e}")

            return {
                "success": True, "tx_hash": tx_hash,
                "mint_price": mint_price, "total_cost": mint_price * amount,
            }

        except Exception as e:
            log.error(f"execute_mint fatal: {e}")
            return {"success": False, "error": str(e)}

    async def monitor_loop(self):
        log.info("🟢 Monitor loop started — checking every 10s")
        while self.monitoring:
            try:
                armed = [p for p in self.projects.values()
                         if p.armed_snipe and not p.armed_snipe.get("executed", False)]
                log.info(f"Monitor tick — {len(self.projects)} projects, {len(armed)} armed")

                for project in armed:
                    contract_addr = project.contract_address
                    snipe = project.armed_snipe

                    is_active = await self.contract_reader.is_mint_active(contract_addr)
                    if not is_active:
                        continue

                    log.info(f"MINT ACTIVE for {project.project_name}!")

                    # Use the specific wallet chosen when arming
                    wallet_address = snipe.get("wallet_address", "")
                    user_wallet = self.wallets.get(wallet_address)

                    # Fallback: any wallet belonging to the user
                    if not user_wallet:
                        user_wallet = next(
                            (w for w in self.wallets.values() if w.added_by == snipe["user_id"]),
                            None
                        )

                    if not user_wallet:
                        await self._notify(project.added_by,
                            f"❌ MINT IS LIVE but no wallet found!\n\n"
                            f"Project: {project.project_name}\n"
                            f"Add a wallet with /addwallet then re-arm.")
                        continue

                    project.armed_snipe["executed"] = True
                    self.save_data()

                    result = await self.execute_mint(contract_addr, user_wallet, snipe["amount"])

                    if result["success"]:
                        await self._notify(project.added_by, self._success_msg(project, result))
                    else:
                        project.armed_snipe["executed"] = False
                        project.armed_snipe["fail_count"] = snipe.get("fail_count", 0) + 1
                        project.armed_snipe["last_error"] = result.get("error", "")
                        self.save_data()
                        balance = self.get_wallet_balance(user_wallet.address)
                        await self._notify(project.added_by, self._fail_msg(project, result, balance))

            except Exception as e:
                log.error(f"monitor_loop error: {e}")

            await asyncio.sleep(10)

    async def _notify(self, chat_id: int, text: str):
        if not self.bot_app:
            return
        try:
            await self.bot_app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            log.warning(f"Markdown failed, trying plain: {e}")
            try:
                plain = text.replace("*","").replace("`","").replace("_","")
                await self.bot_app.bot.send_message(chat_id=chat_id, text=plain)
            except Exception as e2:
                log.error(f"_notify failed: {e2}")

    def _success_msg(self, project: TrackedProject, result: Dict) -> str:
        snipe = project.armed_snipe or {}
        return (
            f"✅ MINT SUCCESSFUL!\n\n"
            f"Project: {project.project_name}\n"
            f"Stage: {snipe.get('stage_name', '?')}\n"
            f"Minted: {snipe.get('amount', '?')} NFT(s)\n"
            f"Price: {result['mint_price']} ETH each\n"
            f"Total: {result['total_cost']} ETH\n"
            f"Tx: {result['tx_hash'][:20]}...\n\n"
            f"Check your wallet!"
        )

    def _fail_msg(self, project: TrackedProject, result: Dict, balance_eth: float = 0.0) -> str:
        raw_error = result.get("error", "Unknown")
        safe_error = raw_error.replace("*","").replace("`","").replace("_","").replace("{","").replace("}","")[:300]
        snipe = project.armed_snipe or {}

        if "insufficient funds" in raw_error.lower():
            diagnosis = (
                f"Wallet only has {balance_eth:.6f} ETH.\n"
                f"Top up and bot will retry automatically."
            )
        elif "nonce" in raw_error.lower():
            diagnosis = "Nonce mismatch — pending tx may be stuck. Retrying."
        elif "No selector succeeded" in raw_error:
            diagnosis = "Could not find mint function. Check contract address."
        elif "replacement transaction underpriced" in raw_error.lower():
            diagnosis = "Pending tx with higher gas exists. Retrying."
        else:
            diagnosis = "Bot will retry automatically next cycle."

        return (
            f"❌ MINT TRANSACTION FAILED\n\n"
            f"Project: {project.project_name}\n"
            f"Stage: {snipe.get('stage_name', '?')}\n"
            f"Amount: {snipe.get('amount', '?')} NFT(s)\n"
            f"Wallet balance: {balance_eth:.6f} ETH\n"
            f"Attempt: #{snipe.get('fail_count', 1)}\n\n"
            f"Error: {safe_error}\n\n"
            f"Diagnosis: {diagnosis}\n\n"
            f"Use /cancel <contract> to stop retrying."
        )

# ============ GLOBAL ============
monitor = ProjectMonitor()

# ============ HELPERS ============
def wallet_keyboard(wallets: List[Wallet], prefix: str) -> InlineKeyboardMarkup:
    """Build inline keyboard with one button per wallet."""
    buttons = [
        [InlineKeyboardButton(
            f"{w.address[:10]}...{w.address[-6:]} | {monitor.get_wallet_balance(w.address):.4f} ETH",
            callback_data=f"{prefix}:{w.address}"
        )]
        for w in wallets
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(buttons)

# ============ START / HELP ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add Wallet",    callback_data="add_wallet"),
         InlineKeyboardButton("🗑 Remove Wallet", callback_data="remove_wallet")],
        [InlineKeyboardButton("👛 My Wallets",    callback_data="view_wallets")],
        [InlineKeyboardButton("📊 Track Project", callback_data="track_project")],
        [InlineKeyboardButton("🎯 Auto-Mint",     callback_data="arm_snipe"),
         InlineKeyboardButton("🚀 Manual Mint",   callback_data="manual_mint")],
        [InlineKeyboardButton("📋 My Projects",   callback_data="list_projects")],
        [InlineKeyboardButton("❌ Cancel Snipe",  callback_data="cancel_snipe")],
        [InlineKeyboardButton("⛽ Gas",            callback_data="gas")],
    ]
    await update.message.reply_text(
        f"🤖 *NFT SNIPER BOT*\n\n"
        f"💰 *Fee:* {FEE_PERCENTAGE}%  |  🔗 *Chain:* Ethereum\n\n"
        f"*Mint Modes:*\n"
        f"🎯 *Auto-Mint* — arm it, bot fires when mint goes live\n"
        f"🚀 *Manual Mint* — send tx right now to any contract\n\n"
        f"*Quick Start:*\n"
        f"1️⃣ `/addwallet` — add wallet\n"
        f"2️⃣ `/mint <contract> <price> <amount>` — manual mint NOW\n"
        f"   or `/track` → `/addstage` → `/snipe` — auto-mint\n\n"
        f"Type /help for all commands",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 *Commands*\n\n"
        "*Wallets*\n"
        "• `/addwallet` — add a wallet\n"
        "• `/wallets` — view wallets + balances\n"
        "• `/removewallet` — remove a wallet\n\n"
        "*Manual Mint (no tracking needed)*\n"
        "• `/mint <contract> <price_eth> <amount>` — mint right now\n"
        "  Example: `/mint 0x123... 0.0016 1`\n\n"
        "*Auto-Mint (monitors & fires when live)*\n"
        "• `/track <contract> <name>` — track project\n"
        "• `/addstage <contract> <stage> <price> <max>`\n"
        "• `/snipe <contract> <stage> <amount>` — arm auto-mint\n"
        "• `/cancel <contract>` — cancel auto-mint\n\n"
        "*Info*\n"
        "• `/stages <contract>` — view stages\n"
        "• `/list` — all tracked projects\n"
        "• `/stats <contract>` — live stats\n"
        "• `/gas` — gas prices\n"
    )
    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text(text, parse_mode="Markdown")

# ============ BUTTON CALLBACKS ============
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cmd = query.data
    uid = update.effective_user.id

    # --- wallet selection for remove ---
    if cmd.startswith("removewallet:"):
        address = cmd.split(":", 1)[1]
        if address == "cancel":
            await query.message.edit_text("❌ Cancelled.")
            return
        if monitor.remove_wallet(address, uid):
            await query.message.edit_text(f"✅ Wallet removed:\n{address[:15]}...{address[-8:]}")
        else:
            await query.message.edit_text("❌ Could not remove wallet.")
        return

    # --- wallet selection for snipe ---
    if cmd.startswith("snipewallet:"):
        address = cmd.split(":", 1)[1]
        if address == "cancel":
            await query.message.edit_text("❌ Cancelled.")
            context.user_data.clear()
            return
        pending = context.user_data.get("pending_snipe")
        if not pending:
            await query.message.edit_text("❌ Session expired. Run /snipe again.")
            return
        monitor.arm_snipe(
            pending["contract"], pending["stage"], pending["amount"], uid, address
        )
        balance = monitor.get_wallet_balance(address)
        project = monitor.projects[pending["contract"]]
        await query.message.edit_text(
            f"🎯 Auto-Mint Armed!\n\n"
            f"Project: {project.project_name}\n"
            f"Stage: {pending['stage']}\n"
            f"Amount: {pending['amount']} NFT(s)\n"
            f"Wallet: {address[:12]}...{address[-6:]} ({balance:.4f} ETH)\n\n"
            f"Bot checks every 10s — will fire when mint goes live!"
        )
        context.user_data.clear()
        return

    # --- wallet selection for manual mint ---
    if cmd.startswith("mintwallet:"):
        address = cmd.split(":", 1)[1]
        if address == "cancel":
            await query.message.edit_text("❌ Cancelled.")
            context.user_data.clear()
            return
        pending = context.user_data.get("pending_manual_mint")
        if not pending:
            await query.message.edit_text("❌ Session expired. Run /mint again.")
            return
        wallet = monitor.wallets.get(address)
        if not wallet:
            await query.message.edit_text("❌ Wallet not found.")
            return

        await query.message.edit_text(
            f"⏳ Sending mint tx...\n\n"
            f"Contract: {pending['contract'][:16]}...\n"
            f"Price: {pending['price']} ETH\n"
            f"Amount: {pending['amount']} NFT(s)\n"
            f"Wallet: {address[:12]}...{address[-6:]}"
        )

        result = await monitor.execute_mint(
            pending["contract"], wallet, pending["amount"],
            price_override=pending["price"]
        )

        if result["success"]:
            await query.message.edit_text(
                f"✅ MINT SUCCESSFUL!\n\n"
                f"Minted: {pending['amount']} NFT(s)\n"
                f"Price: {result['mint_price']} ETH each\n"
                f"Total: {result['total_cost']} ETH\n"
                f"Tx: {result['tx_hash'][:24]}...\n\n"
                f"Check your wallet!"
            )
        else:
            raw = result.get("error","Unknown")
            safe = raw.replace("{","").replace("}","")[:250]
            balance = monitor.get_wallet_balance(address)
            await query.message.edit_text(
                f"❌ MINT FAILED\n\n"
                f"Wallet balance: {balance:.6f} ETH\n"
                f"Error: {safe}\n\n"
                f"Try a different wallet or top up and retry."
            )
        context.user_data.clear()
        return

    # --- main menu buttons ---
    if cmd == "add_wallet":
        await query.message.reply_text("Send `/addwallet` to add a wallet", parse_mode="Markdown")
    elif cmd == "remove_wallet":
        wallets = monitor.get_user_wallets(uid)
        if not wallets:
            await query.message.reply_text("No wallets to remove.")
        else:
            await query.message.reply_text(
                "Select wallet to remove:",
                reply_markup=wallet_keyboard(wallets, "removewallet")
            )
    elif cmd == "view_wallets":
        wallets = monitor.get_user_wallets(uid)
        if not wallets:
            await query.message.reply_text("No wallets. Use /addwallet")
        else:
            msg = "💼 Your Wallets\n\n"
            for w in wallets:
                bal = monitor.get_wallet_balance(w.address)
                msg += f"{w.address[:14]}...{w.address[-6:]} | {bal:.4f} ETH\n"
            await query.message.reply_text(msg)
    elif cmd == "track_project":
        await query.message.reply_text("Send: `/track <contract> <name>`", parse_mode="Markdown")
    elif cmd == "arm_snipe":
        await query.message.reply_text("Send: `/snipe <contract> <stage> <amount>`", parse_mode="Markdown")
    elif cmd == "manual_mint":
        await query.message.reply_text(
            "🚀 Manual Mint — fires immediately!\n\n"
            "Send: `/mint <contract> <price_eth> <amount>`\n\n"
            "Example: `/mint 0x123... 0.0016 1`",
            parse_mode="Markdown"
        )
    elif cmd == "list_projects":
        if not monitor.projects:
            await query.message.reply_text("No projects tracked.")
        else:
            msg = "📋 Tracked Projects\n\n"
            for addr, p in monitor.projects.items():
                status = "🔫 Armed" if p.armed_snipe and not p.armed_snipe.get("executed") else "⚡ Watching"
                msg += f"{p.project_name} — {status}\n{addr[:14]}...\n\n"
            await query.message.reply_text(msg)
    elif cmd == "cancel_snipe":
        await query.message.reply_text("Send: `/cancel <contract>`", parse_mode="Markdown")
    elif cmd == "gas":
        await _send_gas(query.message)

# ============ WALLET COMMANDS ============
async def add_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💳 Send your private key (0x...)\n🔒 Encrypted on save.\n\n/cancel to abort."
    )
    return ADD_WALLET

async def add_wallet_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip()
    if len(key) < 30:
        await update.message.reply_text("❌ Invalid key. Try again or /cancel")
        return ADD_WALLET
    try:
        address = monitor.add_wallet(key, update.effective_user.id)
        bal = monitor.get_wallet_balance(address)
        await update.message.reply_text(
            f"✅ Wallet Added!\n\n"
            f"Address: {address[:14]}...{address[-8:]}\n"
            f"Balance: {bal:.6f} ETH"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallets = monitor.get_user_wallets(update.effective_user.id)
    if not wallets:
        await update.message.reply_text("No wallets. Use /addwallet")
        return
    msg = "💼 Your Wallets\n\n"
    for w in wallets:
        bal = monitor.get_wallet_balance(w.address)
        msg += f"{w.address[:14]}...{w.address[-8:]} | {bal:.6f} ETH\n"
    await update.message.reply_text(msg)

async def remove_wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show wallet picker to remove."""
    wallets = monitor.get_user_wallets(update.effective_user.id)
    if not wallets:
        await update.message.reply_text("No wallets to remove.")
        return
    await update.message.reply_text(
        "Select the wallet you want to remove:",
        reply_markup=wallet_keyboard(wallets, "removewallet")
    )

# ============ MANUAL MINT ============
async def mint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /mint <contract> <price_eth> <amount>
    Fires immediately — no tracking needed.
    """
    uid = update.effective_user.id

    if len(context.args) < 3:
        await update.message.reply_text(
            "🚀 Manual Mint\n\n"
            "Usage: /mint <contract> <price_eth> <amount>\n\n"
            "Example: /mint 0x52c52926cf3122d1eafff8340e4c158ffad9dd43 0.0016 1\n\n"
            "No tracking or stage setup needed — sends tx immediately."
        )
        return

    contract = context.args[0].lower().strip()
    try:
        price = float(context.args[1])
        amount = int(context.args[2])
        if amount < 1 or amount > 50: raise ValueError
    except Exception:
        await update.message.reply_text("❌ Invalid price or amount (amount must be 1–50)")
        return

    # Validate contract address
    try:
        web3.to_checksum_address(contract)
    except Exception:
        await update.message.reply_text("❌ Invalid contract address")
        return

    wallets = monitor.get_user_wallets(uid)
    if not wallets:
        await update.message.reply_text("❌ No wallet found. Use /addwallet first!")
        return

    # Save pending mint in user_data
    context.user_data["pending_manual_mint"] = {
        "contract": contract, "price": price, "amount": amount
    }

    if len(wallets) == 1:
        # Only one wallet — use it directly via fake callback
        wallet = wallets[0]
        balance = monitor.get_wallet_balance(wallet.address)
        msg = await update.message.reply_text(
            f"⏳ Sending mint tx...\n\n"
            f"Contract: {contract[:16]}...\n"
            f"Price: {price} ETH\n"
            f"Amount: {amount} NFT(s)\n"
            f"Wallet: {wallet.address[:12]}...{wallet.address[-6:]} ({balance:.4f} ETH)"
        )
        result = await monitor.execute_mint(contract, wallet, amount, price_override=price)
        if result["success"]:
            await msg.edit_text(
                f"✅ MINT SUCCESSFUL!\n\n"
                f"Minted: {amount} NFT(s)\n"
                f"Price: {result['mint_price']} ETH each\n"
                f"Total: {result['total_cost']} ETH\n"
                f"Tx: {result['tx_hash'][:24]}...\n\n"
                f"Check your wallet!"
            )
        else:
            raw = result.get("error","Unknown")
            safe = raw.replace("{","").replace("}","")[:250]
            balance = monitor.get_wallet_balance(wallet.address)
            await msg.edit_text(
                f"❌ MINT FAILED\n\n"
                f"Wallet balance: {balance:.6f} ETH\n"
                f"Error: {safe}\n\n"
                f"Top up your wallet or try /mint again with a different wallet."
            )
        context.user_data.clear()
    else:
        # Multiple wallets — let user pick
        await update.message.reply_text(
            f"Select wallet to mint with:\n"
            f"(Contract: {contract[:14]}... | Price: {price} ETH | Amount: {amount})",
            reply_markup=wallet_keyboard(wallets, "mintwallet")
        )

# ============ AUTO-MINT (SNIPE) ============
async def track_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /track <contract> <name>")
        return
    contract, name = context.args[0].strip(), " ".join(context.args[1:])
    if monitor.add_project(contract, name, [], update.effective_user.id):
        await update.message.reply_text(
            f"✅ Tracking {name}!\n\n"
            f"Now add a stage:\n"
            f"/addstage {contract[:14]}... <stage> <price> <max>\n\n"
            f"Then arm auto-mint:\n"
            f"/snipe {contract[:14]}... <stage> <amount>"
        )
    else:
        await update.message.reply_text("⚠️ Already tracking this contract.")

async def addstage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 4:
        await update.message.reply_text(
            "Usage: /addstage <contract> <stage> <price> <max>\n"
            "Example: /addstage 0x... PUBLIC 0.0016 1"
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
        await update.message.reply_text("❌ Not tracked. Use /track first.")
        return
    monitor.projects[contract].stages.append({
        "name": stage_name, "price_eth": price,
        "max_per_wallet": max_pw, "is_active": False, "is_minted": False
    })
    monitor.save_data()
    await update.message.reply_text(
        f"✅ Stage {stage_name} added — {price} ETH, max {max_pw}\n\n"
        f"Now arm: /snipe {contract[:14]}... {stage_name} <amount>"
    )

async def snipe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /snipe <contract> <stage> <amount>\n\n"
            "Tip: Use /mint for immediate minting without tracking!"
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
        await update.message.reply_text(
            "❌ Contract not tracked.\n\n"
            "Use /track first, OR use /mint for immediate minting without tracking."
        )
        return

    stage_data = next(
        (s for s in monitor.projects[contract].stages if s.get("name","").upper() == stage),
        None
    )
    if not stage_data:
        await update.message.reply_text(
            f"❌ Stage '{stage}' not found.\n"
            f"Add it: /addstage {contract[:14]}... {stage} <price> <max>"
        )
        return

    wallets = monitor.get_user_wallets(uid)
    if not wallets:
        await update.message.reply_text("❌ No wallet. Use /addwallet first!")
        return

    # Save pending snipe
    context.user_data["pending_snipe"] = {
        "contract": contract, "stage": stage, "amount": amount
    }

    if len(wallets) == 1:
        # One wallet — arm directly
        monitor.arm_snipe(contract, stage, amount, uid, wallets[0].address)
        bal = monitor.get_wallet_balance(wallets[0].address)
        project = monitor.projects[contract]
        await update.message.reply_text(
            f"🎯 Auto-Mint Armed!\n\n"
            f"Project: {project.project_name}\n"
            f"Stage: {stage}\n"
            f"Price: {stage_data.get('price_eth', 0)} ETH\n"
            f"Amount: {amount} NFT(s)\n"
            f"Wallet: {wallets[0].address[:12]}...{wallets[0].address[-6:]} ({bal:.4f} ETH)\n\n"
            f"⚡ Monitoring every 10 seconds!"
        )
        context.user_data.clear()
    else:
        # Multiple wallets — let user pick
        await update.message.reply_text(
            f"Select which wallet to use for auto-mint:\n"
            f"(Stage: {stage} | Amount: {amount})",
            reply_markup=wallet_keyboard(wallets, "snipewallet")
        )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /cancel <contract>")
        return
    contract = context.args[0].lower().strip()
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Not tracked.")
        return
    if monitor.projects[contract].armed_snipe:
        monitor.disarm_snipe(contract)
        await update.message.reply_text("✅ Auto-mint cancelled.")
    else:
        await update.message.reply_text("ℹ️ No active snipe for that contract.")

# ============ INFO COMMANDS ============
async def stages_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /stages <contract>")
        return
    contract = context.args[0].lower().strip()
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Not tracked.")
        return
    project = monitor.projects[contract]
    if not project.stages:
        await update.message.reply_text("No stages yet. Use /addstage")
        return
    msg = f"🎟 {project.project_name} Stages\n\n"
    for s in project.stages:
        msg += f"{s['name']} — {s['price_eth']} ETH | Max {s['max_per_wallet']}\n"
    await update.message.reply_text(msg)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /stats <contract>")
        return
    contract = context.args[0].lower().strip()
    if contract not in monitor.projects:
        await update.message.reply_text("❌ Not tracked.")
        return
    msg = await update.message.reply_text("🔄 Checking...")
    is_active = await monitor.contract_reader.is_mint_active(contract)
    mint_price = await monitor.contract_reader.get_mint_price(contract)
    project = monitor.projects[contract]
    stages_text = "\n".join(f"• {s['name']} — {s['price_eth']} ETH" for s in project.stages) or "None"
    armed = project.armed_snipe and not project.armed_snipe.get("executed")
    await msg.edit_text(
        f"📊 {project.project_name}\n\n"
        f"Contract deployed: {'✅' if is_active else '❌'}\n"
        f"Detected price: {mint_price} ETH\n\n"
        f"Stages:\n{stages_text}\n\n"
        f"Auto-mint: {'✅ Armed' if armed else '❌ Not armed'}"
    )

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitor.projects:
        await update.message.reply_text("No projects tracked.")
        return
    msg = "📋 Tracked Projects\n\n"
    for addr, p in monitor.projects.items():
        status = "🔫 Armed" if p.armed_snipe and not p.armed_snipe.get("executed") else "⚡ Watching"
        msg += f"{p.project_name} — {status}\n{addr[:14]}...\n\n"
    await update.message.reply_text(msg)

async def _send_gas(msg_obj):
    try:
        r = requests.get("https://api.etherscan.io/api?module=gastracker&action=gasoracle", timeout=10)
        if r.status_code == 200 and r.json().get("status") == "1":
            g = r.json()["result"]
            await msg_obj.reply_text(
                f"⛽ Gas Prices\n\n"
                f"🐢 Slow: {g['SafeGasPrice']} Gwei\n"
                f"⚡ Standard: {g['ProposeGasPrice']} Gwei\n"
                f"🚀 Fast: {g['FastGasPrice']} Gwei"
            )
            return
    except Exception:
        pass
    await msg_obj.reply_text("⛽ Check: https://etherscan.io/gastracker")

async def gas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_gas(update.message)

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ============ MAIN ============
def main():
    if not TELEGRAM_TOKEN:
        log.error("❌ TELEGRAM_TOKEN not set!")
        return

    log.info(f"Blockchain connected: {web3.is_connected()}")

    async def post_init(app: Application):
        log.info("post_init — starting monitor loop")
        monitor.bot_app = app
        monitor.monitoring = True
        asyncio.create_task(monitor.monitor_loop())

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
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
    app.add_handler(CommandHandler("removewallet", remove_wallet_command))
    app.add_handler(CommandHandler("track", track_command))
    app.add_handler(CommandHandler("addstage", addstage_command))
    app.add_handler(CommandHandler("stages", stages_command))
    app.add_handler(CommandHandler("snipe", snipe_command))
    app.add_handler(CommandHandler("mint", mint_command))
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
