import asyncio
import json
import os
from datetime import datetime, timedelta
import discord
from discord import app_commands
from discord.ext import commands, tasks
import fortnitepy
import firebase_admin
from firebase_admin import credentials, firestore

# ==========================================
# 🔒 הגדרות ומשתני סביבה (ENV VARIABLES)
# ==========================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_DISCORD_ID = int(os.getenv("OWNER_DISCORD_ID", "0"))
DAILY_CREDITS_AMOUNT = 3

# ==========================================
# 🔥 התחברות ל-FIREBASE FIRESTORE
# ==========================================
firebase_json_env = os.getenv("FIREBASE_SERVICE_ACCOUNT")

if firebase_json_env:
    cred_dict = json.loads(firebase_json_env)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
else:
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ==========================================
# 🤖 הגדרת הבוט
# ==========================================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

fortnite_clients = []
logged_in_discord_users = set()

# ==========================================
# 📁 ניהול FIREBASE (חשבונות וקרדיטים)
# ==========================================
def load_accounts_from_firebase():
    accounts_ref = db.collection("fortnite_accounts")
    docs = accounts_ref.stream()
    accounts = []
    for doc in docs:
        accounts.append(doc.to_dict())
    return accounts

def save_account_to_firebase(device_auth_data):
    account_id = device_auth_data["account_id"]
    db.collection("fortnite_accounts").document(account_id).set(device_auth_data)

def remove_account_from_firebase(account_id):
    db.collection("fortnite_accounts").document(account_id).delete()

def get_user_data_from_firebase(user_id: int):
    doc_ref = db.collection("credits").document(str(user_id))
    doc = doc_ref.get()
    if doc.exists:
        return doc.to_dict()
    return {"credits": 0, "last_daily": None}

def get_user_credits(user_id: int) -> int:
    data = get_user_data_from_firebase(user_id)
    return data.get("credits", 0)

def add_user_credits(user_id: int, amount: int):
    str_id = str(user_id)
    doc_ref = db.collection("credits").document(str_id)
    data = get_user_data_from_firebase(user_id)
    new_credits = max(0, data.get("credits", 0) + amount)
    doc_ref.set({"credits": new_credits, "last_daily": data.get("last_daily")}, merge=True)

def set_user_credits(user_id: int, amount: int):
    str_id = str(user_id)
    doc_ref = db.collection("credits").document(str_id)
    doc_ref.set({"credits": amount}, merge=True)

def get_user_daily_time(user_id: int):
    data = get_user_data_from_firebase(user_id)
    last_daily_str = data.get("last_daily")
    if last_daily_str:
        return datetime.fromisoformat(last_daily_str)
    return None

def set_user_daily_time(user_id: int, dt: datetime):
    str_id = str(user_id)
    doc_ref = db.collection("credits").document(str_id)
    doc_ref.set({"last_daily": dt.isoformat()}, merge=True)

# ==========================================
# 🔄 KEEP-ALIVE TASK & CHARACTERS LOAD
# ==========================================
@tasks.loop(minutes=30)
async def keep_alive_task():
    for client in fortnite_clients:
        try:
            if client.is_ready():
                await client.fetch_profile()
        except Exception as e:
            print(f"⚠️ Keep-alive ping failed: {e}")

async def load_saved_fortnite_clients():
    accounts = load_accounts_from_firebase()
    expired_accounts = []
    print(f"🔄 טוען {len(accounts)} חשבונות שמורים מ-Firebase...")

    for acc in accounts:
        try:
            auth = fortnitepy.DeviceAuth(
                account_id=acc["account_id"],
                device_id=acc["device_id"],
                secret=acc["secret"]
            )
            client = fortnitepy.Client(auth=auth)
            await client.start()
            fortnite_clients.append(client)
            print(f"✅ מחובר: {client.user.display_name}")
        except Exception as e:
            print(f"❌ שגיאה בטעינת חשבון {acc.get('account_id')}: {e}")
            expired_accounts.append(acc.get("account_id"))

    if expired_accounts:
        for acc_id in expired_accounts:
            remove_account_from_firebase(acc_id)
        print(f"🗑️ נוקו {len(expired_accounts)} חשבונות פגי תוקף מ-Firebase.")

    if not keep_alive_task.is_running():
        keep_alive_task.start()

# ==========================================
# 🎨 PROGRESS BAR HELPER
# ==========================================
def render_progress_bar(current, total, length=20):
    percent = current / total if total > 0 else 0
    filled_length = int(length * percent)
    bar = "🟩" * filled_length + "⬛" * (length - filled_length)
    return bar

# ==========================================
# 📝 MODAL (טופס להזנת שם וסיבובים)
# ==========================================
class SpamModal(discord.ui.Modal, title="🚀 התחלת ספאם בקשות חברות"):
    username = discord.ui.TextInput(
        label="שם משתמש בפורטנייט (Epic Username)",
        placeholder="הכנס את השם בדיוק כפי שמופיע במשחק...",
        required=True
    )
    rounds = discord.ui.TextInput(
        label="מספר סיבובים (1 קרדיט = סיבוב מלא מכל החשבונות)",
        placeholder="ברירת מחדל: 1",
        default="1",
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rounds_val = int(self.rounds.value)
        except ValueError:
            await interaction.response.send_message("❌ מספר הסיבובים חייב להיות מספר שלם!", ephemeral=True)
            return

        await run_spammer_logic(interaction, self.username.value.strip(), rounds_val)

# ==========================================
# 🎛️ PANEL VIEW & BUTTONS (כפתורי הפאנל)
# ==========================================
class SpamPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start Attack", style=discord.ButtonStyle.danger, emoji="🚀", custom_id="btn_start_attack")
    async def start_attack_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SpamModal())

    @discord.ui.button(label="Credits", style=discord.ButtonStyle.secondary, emoji="💰", custom_id="btn_check_credits")
    async def credits_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == OWNER_DISCORD_ID:
            user_credits = "♾️ (ללא הגבלה)"
        else:
            user_credits = f"**{get_user_credits(interaction.user.id)}**"

        embed = discord.Embed(
            title="💳 יתרת קרדיטים",
            description=f"משתמש: {interaction.user.mention}\nיתרה: {user_credits} קרדיטים",
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Buy Credits / Daily", style=discord.ButtonStyle.primary, emoji="🛒", custom_id="btn_buy_credits")
    async def buy_credits_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        last_daily = get_user_daily_time(user_id)
        now = datetime.utcnow()

        if last_daily and (now - last_daily) < timedelta(hours=24):
            next_claim = last_daily + timedelta(hours=24)
            remaining = next_claim - now
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)

            embed = discord.Embed(
                title="⏳ פרס יומי - כבר אספת!",
                description=f"תוכל לאסוף שוב 3 קרדיטים בעוד **{hours} שעות ו-{minutes} דקות**.\n\nלקניית קרדיטים נוספים צור קשר עם מנהל השרת!",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        add_user_credits(user_id, DAILY_CREDITS_AMOUNT)
        set_user_daily_time(user_id, now)
        new_total = get_user_credits(user_id)

        embed = discord.Embed(
            title="🎁 קיבלת את הפרס היומי!",
            description=f"נוספו לחשבונך **{DAILY_CREDITS_AMOUNT}** קרדיטים!\nיתרה מעודכנת: **{new_total}** קרדיטים.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ==========================================
# ⚡ לוגיקת הרצת הספאם (SPAMMER LOGIC)
# ==========================================
async def run_spammer_logic(interaction: discord.Interaction, username: str, rounds: int):
    if interaction.user.id not in logged_in_discord_users and interaction.user.id != OWNER_DISCORD_ID:
        await interaction.response.send_message(
            "❌ **אין לך הרשאה!**\nיש להתחבר תחילה באמצעות `/login`.", 
            ephemeral=True
        )
        return

    if not fortnite_clients:
        await interaction.response.send_message("⚠️ אין כרגע אף חשבון מחובר במערכת.", ephemeral=True)
        return

    if rounds <= 0:
        await interaction.response.send_message("❌ מספר הסיבובים חייב להיות חיובי.", ephemeral=True)
        return

    accounts_in_pool = len(fortnite_clients)
    required_credits = rounds
    user_credits = get_user_credits(interaction.user.id)

    if interaction.user.id != OWNER_DISCORD_ID and user_credits < required_credits:
        embed_no_credits = discord.Embed(
            title="❌ אין מספיק קרדיטים",
            description=f"אתה צריך **{required_credits}** קרדיטים עבור **{rounds}** סיבובים ({rounds * accounts_in_pool} בקשות חברות בסך הכל).\n\nיתרתך כרגע: **{user_credits}** קרדיטים.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed_no_credits, ephemeral=True)
        return

    await interaction.response.defer()

    target_user = None
    for client in fortnite_clients:
        try:
            target_user = await client.fetch_user(username)
            if target_user:
                break
        except Exception:
            continue

    if not target_user:
        embed_error = discord.Embed(
            description=f"❌ השחקן **{username}** לא נמצא ב-Epic Games.",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed_error)
        return

    target_name = target_user.display_name
    target_id = target_user.id
    
    total_expected_requests = rounds * accounts_in_pool
    sent_count = 0
    failed_count = 0

    def build_progress_embed(sent, failed, total_done):
        bar = render_progress_bar(total_done, total_expected_requests, length=20)
        embed = discord.Embed(
            title="📨 Spamming in progress...",
            color=discord.Color.blue()
        )
        embed.add_field(name="🎯 Target:", value=f"`{target_name}`", inline=False)
        embed.add_field(name="👤 Spammed by:", value=interaction.user.mention, inline=False)
        embed.add_field(name="🔑 Active accounts:", value=f"`{accounts_in_pool} / {accounts_in_pool}`", inline=False)
        embed.add_field(
            name="Progress", 
            value=f"{bar}\n\n✅ **Sent:** {sent} | ❌ **Failed:** {failed} | 📦 **Total:** {total_done} / {total_expected_requests}", 
            inline=False
        )
        return embed

    message = await interaction.followup.send(embed=build_progress_embed(0, 0, 0))

    for r in range(rounds):
        for client in fortnite_clients:
            try:
                await client.add_friend(target_id)
                sent_count += 1
            except Exception:
                failed_count += 1

            total_done = sent_count + failed_count
            if total_done % 2 == 0 or total_done >= total_expected_requests:
                try:
                    await message.edit(embed=build_progress_embed(sent_count, failed_count, total_done))
                except Exception:
                    pass

            await asyncio.sleep(0.1)

    if interaction.user.id != OWNER_DISCORD_ID:
        add_user_credits(interaction.user.id, -rounds)
        remaining_credits = get_user_credits(interaction.user.id)
    else:
        remaining_credits = "♾️"

    final_embed = discord.Embed(
        title="✅ Spam Complete!",
        color=discord.Color.green()
    )
    final_embed.add_field(name="🎯 Target:", value=f"`{target_name}`", inline=False)
    final_embed.add_field(name="👤 Spammed by:", value=interaction.user.mention, inline=False)
    final_embed.add_field(name="✅ Friend requests sent:", value=f"`{sent_count}`", inline=False)
    final_embed.add_field(name="❌ Failed:", value=f"`{failed_count}`", inline=False)
    final_embed.add_field(name="💳 Credits Used:", value=f"`{rounds}` (Remaining: `{remaining_credits}`)", inline=False)
    final_embed.set_footer(text=f"`{target_name}` received {sent_count} friend request notifications in-game.")

    await message.edit(embed=final_embed)

# ==========================================
# 💻 SLASH COMMANDS
# ==========================================

@bot.tree.command(name="setup", description="שלח את פאנל הכפתורים של הבוט (למנהלים בלבד)")
async def setup_panel(interaction: discord.Interaction):
    if interaction.user.id != OWNER_DISCORD_ID:
        await interaction.response.send_message("❌ אין לך הרשאה לבצע פקודה זו.", ephemeral=True)
        return

    embed = discord.Embed(
        title="💣 NL Spam Panel",
        description="המערכת הטובה במדינה\n\n"
                    "🚀 **Start Attack** – פותח את הטופס לשליחת שם המשתמש והקרדיטים\n"
                    "💰 **Credits** – מציג את כמות הקרדיטים שלך\n"
                    "🛒 **Buy Credits** – פותח את עמוד הקניות / איסוף פרס יומי\n\n"
                    "**מהיר, פשוט, איכותי | NL**",
        color=discord.Color.blue()
    )
    await interaction.channel.send(embed=embed, view=SpamPanelView())
    await interaction.response.send_message("✅ פאנל נשלח בהצלחה!", ephemeral=True)

@bot.tree.command(name="spammer", description="שלח ספאם של בקשות חברות באמצעות פקודה")
@app_commands.describe(username="שם המשתמש בפורטנייט לקבלת הספאם", rounds="מספר הסיבובים (קרדיטים)")
async def spammer_cmd(interaction: discord.Interaction, username: str, rounds: int = 1):
    await run_spammer_logic(interaction, username, rounds)

@bot.tree.command(name="login", description="התחבר לחשבון Epic והוסף אותו לבוט")
@app_commands.describe(code="קוד האימות מ-Epic Games")
async def login(interaction: discord.Interaction, code: str = None):
    login_url = "https://www.epicgames.com/id/api/redirect?clientId=3446500fed614bb29778276da0e07982&responseType=code"

    if not code:
        embed = discord.Embed(
            title="🔗 התחברות לחשבון Epic Games",
            description="1. כנס לקישור והתחבר לחשבון ה-Epic שלך:\n"
                        f"[לחץ כאן להוצאת קוד]({login_url})\n\n"
                        "2. העתק את ה-`authorizationCode` מתוך הדף שייפתח.\n"
                        "3. הרץ שוב: `/login code:הקוד_שלך`",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await interaction.response.send_message("⏳ מתחבר לחשבון Epic Games...", ephemeral=True)

    try:
        auth = fortnitepy.AdvancedAuth(code=code)
        client = fortnitepy.Client(auth=auth)
        await client.start()

        device_data = {
            "account_id": client.auth.device_auth.account_id,
            "device_id": client.auth.device_auth.device_id,
            "secret": client.auth.device_auth.secret
        }
        
        save_account_to_firebase(device_data)
        fortnite_clients.append(client)
        logged_in_discord_users.add(interaction.user.id)

        display_name = client.user.display_name
        await interaction.edit_original_response(
            content=f"🎉 **התחברת בהצלחה!** החשבון **{display_name}** נשמר ב-Firebase ונוסף למאגר."
        )

    except Exception as e:
        await interaction.edit_original_response(content=f"❌ ההתחברות נכשלה.\nשגיאה: `{e}`")

@bot.tree.command(name="credits", description="בדוק את יתרת הקרדיטים שלך")
async def check_credits_cmd(interaction: discord.Interaction):
    if interaction.user.id == OWNER_DISCORD_ID:
        user_credits = "♾️ (ללא הגבלה)"
    else:
        user_credits = f"**{get_user_credits(interaction.user.id)}**"

    embed = discord.Embed(
        title="💳 יתרת קרדיטים",
        description=f"משתמש: {interaction.user.mention}\nיתרה: {user_credits} קרדיטים",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="daily", description="קבל את הפרס היומי שלך (3 קרדיטים)")
async def daily_cmd(interaction: discord.Interaction):
    user_id = interaction.user.id
    last_daily = get_user_daily_time(user_id)
    now = datetime.utcnow()

    if last_daily and (now - last_daily) < timedelta(hours=24):
        next_claim = last_daily + timedelta(hours=24)
        remaining = next_claim - now
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)

        embed = discord.Embed(
            title="⏳ כבר אספת את הפרס היומי!",
            description=f"תוכל לאסוף שוב בעוד **{hours} שעות ו-{minutes} דקות**.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    add_user_credits(user_id, DAILY_CREDITS_AMOUNT)
    set_user_daily_time(user_id, now)
    new_total = get_user_credits(user_id)

    embed = discord.Embed(
        title="🎁 קיבלת את הפרס היומי!",
        description=f"נוספו לחשבונך **{DAILY_CREDITS_AMOUNT}** קרדיטים!\nיתרה מעודכנת: **{new_total}** קרדיטים.",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="addcredits", description="הוסף קרדיטים למשתמש (לבעלים בלבד)")
@app_commands.describe(user="המשתמש", amount="כמות להוספה")
async def add_credits_cmd(interaction: discord.Interaction, user: discord.User, amount: int):
    if interaction.user.id != OWNER_DISCORD_ID:
        await interaction.response.send_message("❌ אין לך הרשאה לבצע פקודה זו.", ephemeral=True)
        return

    add_user_credits(user.id, amount)
    new_total = get_user_credits(user.id)
    await interaction.response.send_message(f"✅ נוספו **{amount}** קרדיטים ל-{user.mention}. יתרה חדשה: **{new_total}**", ephemeral=True)

@bot.tree.command(name="setcredits", description="הגדר קרדיטים למשתמש (לבעלים בלבד)")
@app_commands.describe(user="המשתמש", amount="כמות חדשה")
async def set_credits_cmd(interaction: discord.Interaction, user: discord.User, amount: int):
    if interaction.user.id != OWNER_DISCORD_ID:
        await interaction.response.send_message("❌ אין לך הרשאה לבצע פקודה זו.", ephemeral=True)
        return

    set_user_credits(user.id, amount)
    await interaction.response.send_message(f"⚙️ הוגדרה יתרת **{amount}** קרדיטים ל-{user.mention}.", ephemeral=True)

@bot.tree.command(name="status", description="הצג את מצב הבוט והחשבונות")
async def status_cmd(interaction: discord.Interaction):
    accounts_in_db = len(load_accounts_from_firebase())
    active_count = len(fortnite_clients)
    ping = round(bot.latency * 1000)

    embed = discord.Embed(title="📊 סטטוס המערכת (Firebase Powered)", color=discord.Color.blue())
    embed.add_field(name="🟢 חשבונות פעילים כרגע", value=f"`{active_count}`", inline=True)
    embed.add_field(name="🔥 חשבונות שמורים ב-Firebase", value=f"`{accounts_in_db}`", inline=True)
    embed.add_field(name="⚡ פינג דיסקורד", value=f"`{ping}ms`", inline=True)
    await interaction.response.send_message(embed=embed)

# ==========================================
# 🚀 EVENT ON READY & MAIN
# ==========================================
@bot.event
async def on_ready():
    bot.add_view(SpamPanelView()) # רישום הפאנל מחדש לאחר הפעלה מחדש
    try:
        synced = await bot.tree.sync()
        print(f"🔄 סונכרנו {len(synced)} פקודות Slash.")
    except Exception as e:
        print(f"שגיאה בסנכרון: {e}")
    print(f"🤖 הבוט מוכן בתור {bot.user}")

async def main():
    async with bot:
        asyncio.create_task(load_saved_fortnite_clients())
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
