from handlers.admin_features import AdminFeatures
from modules.access_manager import AccessManager
import json
import logging
import asyncio
import shutil
import os
import re
from datetime import datetime, time
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    CallbackQueryHandler, 
    MessageHandler, 
    filters, 
    ContextTypes, 
    ConversationHandler
)
paris_tz = pytz.timezone('Europe/Paris')

STATS_CACHE = None
LAST_CACHE_UPDATE = None
admin_features = None

# Désactiver les logs de httpx
logging.getLogger("httpx").setLevel(logging.WARNING)

# Configuration du logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Charger la configuration
try:
    with open('config/config.json', 'r', encoding='utf-8') as f:
        CONFIG = json.load(f)
        TOKEN = CONFIG['token']
        ADMIN_IDS = CONFIG['admin_ids']
except FileNotFoundError:
    print("Erreur: Le fichier config.json n'a pas été trouvé!")
    exit(1)
except KeyError as e:
    print(f"Erreur: La clé {e} est manquante dans le fichier config.json!")
    exit(1)

# Fonctions de gestion du catalogue
def load_catalog():
    try:
        with open(CONFIG['catalog_file'], 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_catalog(catalog):
    with open(CONFIG['catalog_file'], 'w', encoding='utf-8') as f:
        json.dump(catalog, f, indent=4, ensure_ascii=False)

def clean_stats():
    """Nettoie les statistiques des produits et catégories qui n'existent plus"""
    if 'stats' not in CATALOG:
        return
    
    stats = CATALOG['stats']
    
    # Nettoyer les vues par catégorie
    if 'category_views' in stats:
        categories_to_remove = []
        for category in stats['category_views']:
            if category not in CATALOG or category == 'stats':
                categories_to_remove.append(category)
        
        for category in categories_to_remove:
            del stats['category_views'][category]
            print(f"🧹 Suppression des stats de la catégorie: {category}")

    # Nettoyer les vues par produit
    if 'product_views' in stats:
        categories_to_remove = []
        for category in stats['product_views']:
            if category not in CATALOG or category == 'stats':
                categories_to_remove.append(category)
                continue
            
            products_to_remove = []
            existing_products = [p['name'] for p in CATALOG[category]]
            
            for product_name in stats['product_views'][category]:
                if product_name not in existing_products:
                    products_to_remove.append(product_name)
            
            # Supprimer les produits qui n'existent plus
            for product in products_to_remove:
                del stats['product_views'][category][product]
                print(f"🧹 Suppression des stats du produit: {product} dans {category}")
            
            # Si la catégorie est vide après nettoyage, la marquer pour suppression
            if not stats['product_views'][category]:
                categories_to_remove.append(category)
        
        # Supprimer les catégories vides
        for category in categories_to_remove:
            if category in stats['product_views']:
                del stats['product_views'][category]

    # Mettre à jour la date de dernière modification
    stats['last_updated'] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    save_catalog(CATALOG)

def get_stats():
    global STATS_CACHE, LAST_CACHE_UPDATE
    current_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    
    # Si le cache existe et a moins de 30 secondes
    if STATS_CACHE and LAST_CACHE_UPDATE and (current_time - LAST_CACHE_UPDATE).seconds < 30:
        return STATS_CACHE
        
    # Sinon, lire le fichier et mettre à jour le cache
    STATS_CACHE = load_catalog()['stats']
    LAST_CACHE_UPDATE = current_time
    return STATS_CACHE

def backup_data():
    """Crée une sauvegarde des fichiers de données"""
    backup_dir = "backups"
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Backup config.json
    if os.path.exists("config/config.json"):
        shutil.copy2("config/config.json", f"{backup_dir}/config_{timestamp}.json")
    
    # Backup catalog.json
    if os.path.exists("config/catalog.json"):
        shutil.copy2("config/catalog.json", f"{backup_dir}/catalog_{timestamp}.json")

def print_catalog_debug():
    """Fonction de debug pour afficher le contenu du catalogue"""
    for category, products in CATALOG.items():
        if category != 'stats':
            print(f"\nCatégorie: {category}")
            for product in products:
                print(f"  Produit: {product['name']}")
                if 'media' in product:
                    print(f"    Médias ({len(product['media'])}): {product['media']}")

# États de conversation
WAITING_FOR_ACCESS_CODE = "WAITING_FOR_ACCESS_CODE"
CHOOSING = "CHOOSING"
WAITING_CATEGORY_NAME = "WAITING_CATEGORY_NAME"
WAITING_PRODUCT_NAME = "WAITING_PRODUCT_NAME"
WAITING_PRODUCT_PRICE = "WAITING_PRODUCT_PRICE"
WAITING_PRODUCT_DESCRIPTION = "WAITING_PRODUCT_DESCRIPTION"
WAITING_PRODUCT_MEDIA = "WAITING_PRODUCT_MEDIA"
SELECTING_CATEGORY = "SELECTING_CATEGORY"
SELECTING_CATEGORY_TO_DELETE = "SELECTING_CATEGORY_TO_DELETE"
SELECTING_PRODUCT_TO_DELETE = "SELECTING_PRODUCT_TO_DELETE"
WAITING_CONTACT_USERNAME = "WAITING_CONTACT_USERNAME"
SELECTING_PRODUCT_TO_EDIT = "SELECTING_PRODUCT_TO_EDIT"
EDITING_PRODUCT_FIELD = "EDITING_PRODUCT_FIELD"
WAITING_NEW_VALUE = "WAITING_NEW_VALUE"
WAITING_BANNER_IMAGE = "WAITING_BANNER_IMAGE"
WAITING_BROADCAST_MESSAGE = "WAITING_BROADCAST_MESSAGE"
WAITING_ORDER_BUTTON_CONFIG = "WAITING_ORDER_BUTTON_CONFIG"
WAITING_WELCOME_MESSAGE = "WAITING_WELCOME_MESSAGE"  # Ajout de cette ligne


# Charger le catalogue au démarrage
CATALOG = load_catalog()

# Fonctions de base

async def handle_access_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la vérification du code d'accès"""
    user_id = update.effective_user.id
    code = update.message.text.strip()
    chat_id = update.effective_chat.id
    
    try:
        # Supprimer le message de l'utilisateur contenant le code
        await update.message.delete()
    except Exception as e:
        pass

    is_valid, reason = access_manager.verify_code(code, user_id)
    
    if is_valid:
        try:
            # Supprimer tous les messages précédents dans le chat (y compris le message de bienvenue)
            current_message_id = update.message.message_id
            
            # Supprimer les 15 derniers messages pour s'assurer que tout est nettoyé
            for i in range(current_message_id - 15, current_message_id + 1):
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=i)
                except Exception as e:
                    pass  # Ignorer silencieusement les erreurs de suppression
                    
            # S'assurer que le message de bienvenue initial est supprimé
            if 'initial_welcome_message_id' in context.user_data:
                try:
                    await context.bot.delete_message(
                        chat_id=chat_id,
                        message_id=context.user_data['initial_welcome_message_id']
                    )
                except Exception as e:
                    pass
                
            # Nettoyer les données stockées
            context.user_data.clear()  # Nettoyer toutes les données stockées
            
        except Exception as e:
            pass  # Ignorer silencieusement les erreurs
        
        # Redirection vers le menu principal sans messages supplémentaires
        return await start(update, context)
    else:
        # Gérer le code invalide avec une popup au lieu d'un message
        error_messages = {
            "expired": "❌ Ce code a expiré",
            "invalid": "❌ Code invalide",
        }
        
        try:
            await update.message.reply_text(
                text=error_messages.get(reason, "Code invalide"),
                reply_markup=None
            )
        except Exception as e:
            pass
            
        return WAITING_FOR_ACCESS_CODE

async def admin_generate_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Génère un nouveau code d'accès (commande admin)"""
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("❌ Cette commande est réservée aux administrateurs.")
        return

    code, expiration = access_manager.generate_code(update.effective_user.id)
    
    # Formater l'expiration
    exp_date = datetime.fromisoformat(expiration)
    exp_str = exp_date.strftime("%d/%m/%Y %H:%M")
    
    await update.message.reply_text(
        f"✅ Nouveau code généré :\n\n"
        f"Code: `{code}`\n"
        f"Expire le: {exp_str}",
        parse_mode='Markdown'
    )

async def admin_list_codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liste tous les codes actifs (commande admin)"""
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("❌ Cette commande est réservée aux administrateurs.")
        return

    active_codes = access_manager.list_active_codes()
    
    if not active_codes:
        await update.message.reply_text("Aucun code actif.")
        return

    message = "📝 Codes actifs :\n\n"
    for code in active_codes:
        exp_date = datetime.fromisoformat(code["expiration"])
        exp_str = exp_date.strftime("%d/%m/%Y %H:%M")
        message += f"Code: `{code['code']}`\n"
        message += f"Expire le: {exp_str}\n\n"

    await update.message.reply_text(message, parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    # Supprimer silencieusement la commande /start si possible
    if hasattr(update, 'message') and update.message:
        try:
            await update.message.delete()
        except Exception:
            pass
    
    # Enregistrer utilisateur
    await admin_features.register_user(user)
    
    # Vérifier si l'utilisateur est autorisé
    if not access_manager.is_authorized(user.id):
        # Supprimer l'ancien message de bienvenue s'il existe
        if 'initial_welcome_message_id' in context.user_data:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=context.user_data['initial_welcome_message_id']
                )
            except Exception:
                pass
        
        # Envoyer le nouveau message de bienvenue
        welcome_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="🔒 Bienvenue ! Pour accéder au bot, veuillez entrer votre code d'accès."
        )
        # Sauvegarder l'ID du message de bienvenue
        context.user_data['initial_welcome_message_id'] = welcome_msg.message_id
        return WAITING_FOR_ACCESS_CODE
    
    # Supprimer les anciens messages si nécessaire
    if 'menu_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=context.user_data['menu_message_id']
            )
        except:
            pass
    
    # Supprimer l'ancienne bannière si elle existe
    if 'banner_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=context.user_data['banner_message_id']
            )
            del context.user_data['banner_message_id']
        except:
            pass
    
    # Nouveau clavier simplifié pour l'accueil
    keyboard = [
        [InlineKeyboardButton("📋 MENU", callback_data="show_categories")]
    ]

    # Définir le texte de bienvenue ici, avant les boutons
    welcome_text = CONFIG.get('welcome_message', 
        "🌿 <b>Bienvenue sur votre bot !</b> 🌿\n\n"
        "<b>Pour changer ce message d accueil, rendez vous dans l onglet admin.</b>\n"
        "📋 Cliquez sur MENU pour voir les catégories"
    )

    # Ajouter le bouton admin si l'utilisateur est administrateur
    if str(update.effective_user.id) in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("🔧 Menu Admin", callback_data="admin")])

    # Configurer le bouton de contact en fonction du type (URL ou username)
    contact_button = None
    if CONFIG.get('contact_url'):
        contact_button = InlineKeyboardButton("📞 Contact", url=CONFIG['contact_url'])
    elif CONFIG.get('contact_username'):
        contact_button = InlineKeyboardButton("📞 Contact Telegram", url=f"https://t.me/{CONFIG['contact_username']}")

    # Ajouter les boutons de contact et canaux
    if contact_button:
        keyboard.extend([
            [
                contact_button,
                InlineKeyboardButton("💭 Tchat telegram", url="https://t.me/+YsJIgYjY8_cyYzBk"),
            ],
            [InlineKeyboardButton("🥔 Canal potato", url="https://doudlj.org/joinchat/QwqUM5gH7Q8VqO3SnS4YwA")]
        ])
    else:
        keyboard.extend([
            [InlineKeyboardButton("💭 Tchat telegram", url="https://t.me/+YsJIgYjY8_cyYzBk")],
            [InlineKeyboardButton("🥔 Canal potato", url="https://doudlj.org/joinchat/QwqUM5gH7Q8VqO3SnS4YwA")]
        ])

    try:
        # Vérifier si une image banner est configurée
        if CONFIG.get('banner_image'):
            banner_message = await context.bot.send_photo(
                chat_id=chat_id,
                photo=CONFIG['banner_image']
            )
            context.user_data['banner_message_id'] = banner_message.message_id

        # Envoyer le menu d'accueil
        menu_message = await context.bot.send_message(
            chat_id=chat_id,
            text=welcome_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'  
        )
        context.user_data['menu_message_id'] = menu_message.message_id
        
    except Exception as e:
        print(f"Erreur lors du démarrage: {e}")
        # En cas d'erreur, envoyer au moins le menu
        menu_message = await context.bot.send_message(
            chat_id=chat_id,
            text=welcome_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
        context.user_data['menu_message_id'] = menu_message.message_id
    
    return CHOOSING

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande pour accéder au menu d'administration"""
    if str(update.effective_user.id) in ADMIN_IDS:
        # Supprimer le message /admin
        await update.message.delete()
        
        # Supprimer les anciens messages si leurs IDs sont stockés
        messages_to_delete = ['menu_message_id', 'banner_message_id', 'category_message_id', 
                            'last_product_message_id', 'instruction_message_id']
        
        for message_key in messages_to_delete:
            if message_key in context.user_data:
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=context.user_data[message_key]
                    )
                    del context.user_data[message_key]
                except Exception as e:
                    print(f"Erreur lors de la suppression du message {message_key}: {e}")
        
        # Envoyer la bannière d'abord si elle existe
        if CONFIG.get('banner_image'):
            try:
                banner_message = await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=CONFIG['banner_image']
                )
                context.user_data['banner_message_id'] = banner_message.message_id
            except Exception as e:
                print(f"Erreur lors de l'envoi de la bannière: {e}")
        
        return await show_admin_menu(update, context)
    else:
        await update.message.reply_text("❌ Vous n'êtes pas autorisé à accéder au menu d'administration.")
        return ConversationHandler.END

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le menu d'administration"""
    is_enabled = access_manager.is_access_code_enabled()
    status_text = "✅ Activé" if is_enabled else "❌ Désactivé"

    keyboard = [
        [InlineKeyboardButton("➕ Ajouter une catégorie", callback_data="add_category")],
        [InlineKeyboardButton("➕ Ajouter un produit", callback_data="add_product")],
        [InlineKeyboardButton("❌ Supprimer une catégorie", callback_data="delete_category")],
        [InlineKeyboardButton("❌ Supprimer un produit", callback_data="delete_product")],
        [InlineKeyboardButton("✏️ Modifier un produit", callback_data="edit_product")],
        [InlineKeyboardButton(f"🔒 Code d'accès: {status_text}", callback_data="toggle_access_code")],
        [InlineKeyboardButton("📊 Statistiques", callback_data="show_stats")],
        [InlineKeyboardButton("📞 Modifier le contact", callback_data="edit_contact")],
        [InlineKeyboardButton("🛒 Modifier bouton Commander", callback_data="edit_order_button")],
        [InlineKeyboardButton("🏠 Modifier message d'accueil", callback_data="edit_welcome")],  
        [InlineKeyboardButton("🖼️ Modifier image bannière", callback_data="edit_banner_image")],
        [InlineKeyboardButton("🔙 Retour à l'accueil", callback_data="back_to_home")]
    ]

    keyboard = await admin_features.add_user_buttons(keyboard)

    admin_text = (
        "🔧 *Menu d'administration*\n\n"
        "Sélectionnez une action à effectuer :"
    )

    try:
        if update.callback_query:
            message = await update.callback_query.edit_message_text(
                admin_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            context.user_data['menu_message_id'] = message.message_id
        else:
            message = await update.message.reply_text(
                admin_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            context.user_data['menu_message_id'] = message.message_id
    except Exception as e:
        print(f"Erreur dans show_admin_menu: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=admin_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

    return CHOOSING

async def handle_order_button_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Gère la configuration du bouton Commander"""
        # Utiliser text_html pour capturer le formatage, sinon utiliser le texte normal
        new_config = update.message.text_html if hasattr(update.message, 'text_html') else update.message.text.strip()
    
        try:
            # Supprimer le message de l'utilisateur
            await update.message.delete()
        
            # Mettre à jour la config selon le format
            if new_config.startswith(('http://', 'https://')):
                CONFIG['order_url'] = new_config
                CONFIG['order_text'] = None
                CONFIG['order_telegram'] = None
                button_type = "URL"
            # Vérifie si c'est un pseudo Telegram (avec ou sans @)
            elif new_config.startswith('@') or not any(c in new_config for c in ' /?=&'):
                # Enlever le @ si présent
                username = new_config[1:] if new_config.startswith('@') else new_config
                CONFIG['order_telegram'] = username
                CONFIG['order_url'] = f"https://t.me/{username}"
                CONFIG['order_text'] = None
                button_type = "Telegram"
            else:
                CONFIG['order_text'] = new_config
                CONFIG['order_url'] = None
                CONFIG['order_telegram'] = None
                button_type = "texte"
            
            # Sauvegarder dans config.json
            with open('config/config.json', 'w', encoding='utf-8') as f:
                json.dump(CONFIG, f, indent=4)
        
            # Supprimer l'ancien message si possible
            if 'edit_order_button_message_id' in context.user_data:
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=context.user_data['edit_order_button_message_id']
                    )
                except:
                    pass
        
            # Message de confirmation avec le @ ajouté si c'est un pseudo Telegram sans @
            display_value = new_config
            if button_type == "Telegram" and not new_config.startswith('@'):
                display_value = f"@{new_config}"
            
            success_message = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"✅ Configuration du bouton Commander mise à jour avec succès!\n\n"
                     f"Type: {button_type}\n"
                     f"Valeur: {display_value}",
                parse_mode='HTML'
            )
        
            # Attendre 3 secondes puis supprimer le message de confirmation
            await asyncio.sleep(3)
            try:
                await success_message.delete()
            except:
                pass
        
            return await show_admin_menu(update, context)
        
        except Exception as e:
            print(f"Erreur dans handle_order_button_config: {e}")
            return WAITING_ORDER_BUTTON_CONFIG

async def handle_banner_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère l'ajout de l'image bannière"""
    if not update.message.photo:
        await update.message.reply_text("Veuillez envoyer une photo.")
        return WAITING_BANNER_IMAGE

    # Supprimer le message précédent
    if 'banner_msg' in context.user_data:
        await context.bot.delete_message(
            chat_id=context.user_data['banner_msg'].chat_id,
            message_id=context.user_data['banner_msg'].message_id
        )
        del context.user_data['banner_msg']

    # Obtenir l'ID du fichier de la photo
    file_id = update.message.photo[-1].file_id
    CONFIG['banner_image'] = file_id

    # Sauvegarder la configuration
    with open('config/config.json', 'w', encoding='utf-8') as f:
        json.dump(CONFIG, f, indent=4)

    # Supprimer le message contenant l'image
    await update.message.delete()

    thread_id = update.message.message_thread_id if update.message.is_topic_message else None

    # Envoyer le message de confirmation
    success_msg = await update.message.reply_text(
        "✅ Image bannière mise à jour avec succès !",
        message_thread_id=thread_id
    )

    # Attendre 3 secondes et supprimer le message
    await asyncio.sleep(3)
    await success_msg.delete()

    return await show_admin_menu(update, context)

async def handle_category_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère l'ajout d'une nouvelle catégorie"""
    category_name = update.message.text.strip()
    
    # Fonction pour compter les emojis
    def count_emojis(text):
        emoji_pattern = re.compile("["
            u"\U0001F600-\U0001F64F"  # emoticons
            u"\U0001F300-\U0001F5FF"  # symbols & pictographs
            u"\U0001F680-\U0001F6FF"  # transport & map symbols
            u"\U0001F1E0-\U0001F1FF"  # flags (iOS)
            u"\U00002702-\U000027B0"
            u"\U000024C2-\U0001F251"
            "]+", flags=re.UNICODE)
        return len(emoji_pattern.findall(text))
    
    # Limites
    MAX_LENGTH = 32  # Longueur maximale du nom de catégorie
    MAX_EMOJIS = 3   # Nombre maximum d'emojis
    MAX_WORDS = 5    # Nombre maximum de mots
    
    # Vérifications
    word_count = len(category_name.split())
    emoji_count = count_emojis(category_name)
    
    error_message = None
    if len(category_name) > MAX_LENGTH:
        error_message = f"❌ Le nom de la catégorie ne doit pas dépasser {MAX_LENGTH} caractères."
    elif word_count > MAX_WORDS:
        error_message = f"❌ Le nom de la catégorie ne doit pas dépasser {MAX_WORDS} mots."
    elif emoji_count > MAX_EMOJIS:
        error_message = f"❌ Le nom de la catégorie ne doit pas contenir plus de {MAX_EMOJIS} emojis."
    elif category_name in CATALOG:
        error_message = "❌ Cette catégorie existe déjà."
    
    if error_message:
        await update.message.reply_text(
            error_message + "\nVeuillez choisir un autre nom:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Annuler", callback_data="cancel_add_category")
            ]])
        )
        return WAITING_CATEGORY_NAME
    
    CATALOG[category_name] = []
    save_catalog(CATALOG)
    
    # Supprimer le message précédent
    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id - 1
    )
    
    # Supprimer le message de l'utilisateur
    await update.message.delete()
    
    return await show_admin_menu(update, context)

async def handle_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère l'entrée du nom du produit"""
    product_name = update.message.text
    category = context.user_data.get('temp_product_category')
    
    if category and any(p.get('name') == product_name for p in CATALOG.get(category, [])):
        await update.message.reply_text(
            "❌ Ce produit existe déjà dans cette catégorie. Veuillez choisir un autre nom:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Annuler", callback_data="cancel_add_product")
            ]])
        )
        return WAITING_PRODUCT_NAME
    
    context.user_data['temp_product_name'] = product_name
    
    # Supprimer le message précédent
    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id - 1
    )
    
    await update.message.reply_text(
        "💰 Veuillez entrer le prix du produit:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Annuler", callback_data="cancel_add_product")
        ]])
    )
    
    # Supprimer le message de l'utilisateur
    await update.message.delete()
    
    return WAITING_PRODUCT_PRICE

async def handle_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère l'entrée du prix du produit"""
    # Utiliser text_html pour capturer le formatage
    price = update.message.text_html if hasattr(update.message, 'text_html') else update.message.text
    context.user_data['temp_product_price'] = price
    
    # Supprimer le message précédent
    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id - 1
    )
    
    await update.message.reply_text(
        "📝 Veuillez entrer la description du produit:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Annuler", callback_data="cancel_add_product")
        ]])
    )
    
    # Supprimer le message de l'utilisateur
    await update.message.delete()
    
    return WAITING_PRODUCT_DESCRIPTION

async def handle_product_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère l'entrée de la description du produit"""
    # Utiliser text_html pour capturer le formatage
    description = update.message.text_html if hasattr(update.message, 'text_html') else update.message.text
    context.user_data['temp_product_description'] = description
    
    # Initialiser la liste des médias
    context.user_data['temp_product_media'] = []
    
    # Supprimer le message précédent
    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id - 1
    )
    
    # Envoyer et sauvegarder l'ID du message d'invitation
    invitation_message = await update.message.reply_text(
        "📸 Envoyez les photos ou vidéos du produit (plusieurs possibles)\n"
        "*Si vous ne voulez pas en envoyer, cliquez sur ignorer* :",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏩ Ignorer", callback_data="skip_media")],
            [InlineKeyboardButton("🔙 Annuler", callback_data="cancel_add_product")]
        ])
    )
    context.user_data['media_invitation_message_id'] = invitation_message.message_id
    
    # Supprimer le message de l'utilisateur
    await update.message.delete()
    
    return WAITING_PRODUCT_MEDIA

async def handle_product_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère l'ajout des médias (photos ou vidéos) du produit"""
    if not (update.message.photo or update.message.video):
        await update.message.reply_text("Veuillez envoyer une photo ou une vidéo.")
        return WAITING_PRODUCT_MEDIA

    if 'temp_product_media' not in context.user_data:
        context.user_data['temp_product_media'] = []

    if 'media_count' not in context.user_data:
        context.user_data['media_count'] = 0

    if 'media_invitation_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['media_invitation_message_id']
            )
            del context.user_data['media_invitation_message_id']
        except Exception as e:
            print(f"Erreur lors de la suppression du message d'invitation: {e}")

    context.user_data['media_count'] += 1

    if update.message.photo:
        media_id = update.message.photo[-1].file_id
        media_type = 'photo'
    else:
        media_id = update.message.video.file_id
        media_type = 'video'

    new_media = {
        'media_id': media_id,
        'media_type': media_type,
        'order_index': context.user_data['media_count']
    }

    context.user_data['temp_product_media'].append(new_media)

    await update.message.delete()

    message = await update.message.reply_text(
        f"Photo/Vidéo {context.user_data['media_count']} ajoutée !\n"
        "Vous pouvez continuer à envoyer des médias ou cliquer sur Terminé pour valider :",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Terminé", callback_data="finish_media")],
            [InlineKeyboardButton("🔙 Annuler", callback_data="cancel_edit")]
        ])
    )
    
    if 'last_confirmation_message_id' in context.user_data:
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['last_confirmation_message_id']
            )
        except:
            pass
            
    context.user_data['last_confirmation_message_id'] = message.message_id

    return WAITING_PRODUCT_MEDIA

async def finish_product_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    category = context.user_data.get('temp_product_category')
    if not category:
        return await show_admin_menu(update, context)

    new_product = {
        'name': context.user_data.get('temp_product_name'),
        'price': context.user_data.get('temp_product_price'),
        'description': context.user_data.get('temp_product_description'),
        'media': context.user_data.get('temp_product_media', [])
    }

    if category not in CATALOG:
        CATALOG[category] = []
    CATALOG[category].append(new_product)
    save_catalog(CATALOG)

    # Au lieu d'essayer de modifier ou supprimer des messages, créons simplement un nouveau menu admin
    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton("➕ Ajouter une catégorie", callback_data="add_category")],
        [InlineKeyboardButton("➕ Ajouter un produit", callback_data="add_product")],
        [InlineKeyboardButton("❌ Supprimer une catégorie", callback_data="delete_category")],
        [InlineKeyboardButton("❌ Supprimer un produit", callback_data="delete_product")],
        [InlineKeyboardButton("✏️ Modifier un produit", callback_data="edit_product")],
        [InlineKeyboardButton("📊 Statistiques", callback_data="show_stats")],
        [InlineKeyboardButton("📞 Modifier le contact", callback_data="edit_contact")],
        [InlineKeyboardButton("🖼️ Modifier image bannière", callback_data="edit_banner_image")],
        [InlineKeyboardButton("🔙 Retour à l'accueil", callback_data="back_to_home")]
    ]

    keyboard = await admin_features.add_user_buttons(keyboard)

    try:
        await query.message.delete()
    except:
        pass

    message = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="🔧 *Menu d'administration*\n\n"
             "✅ Produit ajouté avec succès !\n\n"
             "Sélectionnez une action à effectuer :",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    
    context.user_data['menu_message_id'] = message.message_id
    return CHOOSING

async def handle_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la nouvelle valeur pour le champ en cours de modification"""
    category = context.user_data.get('editing_category')
    product_name = context.user_data.get('editing_product')
    field = context.user_data.get('editing_field')
    
    # Utiliser text_html pour capturer le formatage
    new_value = update.message.text_html if hasattr(update.message, 'text_html') else update.message.text

    if not all([category, product_name, field]):
        await update.message.reply_text("❌ Une erreur est survenue. Veuillez réessayer.")
        return await show_admin_menu(update, context)

    for product in CATALOG.get(category, []):
        if product['name'] == product_name:
            old_value = product.get(field, "Non défini")
            product[field] = new_value
            save_catalog(CATALOG)

            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id - 1
            )
            await update.message.delete()

            keyboard = [[InlineKeyboardButton("🔙 Retour au menu", callback_data="admin")]]
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"✅ Modification effectuée avec succès !\n\n"
                     f"Ancien {field}: {old_value}\n"
                     f"Nouveau {field}: {new_value}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'  # Ajout du parse_mode HTML
            )
            break

    return CHOOSING

async def handle_contact_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la modification du contact"""
    new_value = update.message.text.strip()
    
    try:
        # Supprimer le message de l'utilisateur
        await update.message.delete()
        
        if new_value.startswith(('http://', 'https://')):
            # C'est une URL
            CONFIG['contact_url'] = new_value
            CONFIG['contact_username'] = None
            config_type = "URL"
        else:
            # C'est un pseudo Telegram
            username = new_value.replace("@", "")
            # Vérifier le format basique d'un username Telegram
            if not bool(re.match(r'^[a-zA-Z0-9_]{5,32}$', username)):
                if 'edit_contact_message_id' in context.user_data:
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=context.user_data['edit_contact_message_id'],
                        text="❌ Format d'username Telegram invalide.\n"
                             "L'username doit contenir entre 5 et 32 caractères,\n"
                             "uniquement des lettres, chiffres et underscores (_).\n\n"
                             "Veuillez réessayer:",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔙 Annuler", callback_data="cancel_edit_contact")
                        ]])
                    )
                return WAITING_CONTACT_USERNAME
                
            CONFIG['contact_username'] = username
            CONFIG['contact_url'] = None
            config_type = "Pseudo Telegram"
        
        # Sauvegarder dans config.json
        with open('config/config.json', 'w', encoding='utf-8') as f:
            json.dump(CONFIG, f, indent=4)
        
        # Supprimer l'ancien message de configuration
        if 'edit_contact_message_id' in context.user_data:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=context.user_data['edit_contact_message_id']
                )
            except:
                pass
        
        # Message de confirmation avec le @ ajouté si c'est un pseudo Telegram sans @
        display_value = new_value
        if config_type == "Pseudo Telegram" and not new_value.startswith('@'):
            display_value = f"@{new_value}"
        
        success_message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"✅ Configuration du contact mise à jour avec succès!\n\n"
                 f"Type: {config_type}\n"
                 f"Valeur: {display_value}",
            parse_mode='HTML'
        )
        
        # Attendre 3 secondes puis supprimer le message de confirmation
        await asyncio.sleep(3)
        try:
            await success_message.delete()
        except:
            pass
        
        return await show_admin_menu(update, context)
        
    except Exception as e:
        print(f"Erreur dans handle_contact_username: {e}")
        return WAITING_CONTACT_USERNAME

async def handle_welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la modification du message d'accueil"""
    # Utiliser text_html pour capturer le formatage
    new_message = update.message.text_html if hasattr(update.message, 'text_html') else update.message.text
    
    try:
        # Supprimer le message de l'utilisateur
        await update.message.delete()
        
        # Mettre à jour la config
        CONFIG['welcome_message'] = new_message
        
        # Sauvegarder dans config.json
        with open('config/config.json', 'w', encoding='utf-8') as f:
            json.dump(CONFIG, f, indent=4)
        
        # Supprimer l'ancien message si possible
        if 'edit_welcome_message_id' in context.user_data:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=context.user_data['edit_welcome_message_id']
                )
            except:
                pass
        
        # Message de confirmation
        success_message = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"✅ Message d'accueil mis à jour avec succès!\n\n"
                 f"Nouveau message :\n{new_message}",
            parse_mode='HTML'
        )
        
        # Attendre 3 secondes puis supprimer le message de confirmation
        await asyncio.sleep(3)
        try:
            await success_message.delete()
        except:
            pass
        
        return await show_admin_menu(update, context)
        
    except Exception as e:
        print(f"Erreur dans handle_welcome_message: {e}")
        return WAITING_WELCOME_MESSAGE

async def handle_normal_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestion des boutons normaux"""
    global paris_tz 
    query = update.callback_query
    await query.answer()
    await admin_features.register_user(update.effective_user)


    if query.data == "admin":
        if str(update.effective_user.id) in ADMIN_IDS:
            return await show_admin_menu(update, context)
        else:
            await query.edit_message_text("❌ Vous n'êtes pas autorisé à accéder au menu d'administration.")
            return CHOOSING

    elif query.data == "edit_banner_image":
            msg = await query.message.edit_text(
                "📸 Veuillez envoyer la nouvelle image bannière :",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Annuler", callback_data="cancel_edit")
                ]])
            )
            context.user_data['banner_msg'] = msg
            return WAITING_BANNER_IMAGE

    elif query.data == "manage_users":
        return await admin_features.handle_user_management(update, context)

    elif query.data == "start_broadcast":
        return await admin_features.handle_broadcast(update, context)

    elif query.data == "add_category":
        await query.message.edit_text(
            "📝 Veuillez entrer le nom de la nouvelle catégorie:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Annuler", callback_data="cancel_add_category")
            ]])
        )
        return WAITING_CATEGORY_NAME

    elif query.data == "add_product":
        keyboard = []
        for category in CATALOG.keys():
            if category != 'stats':
                keyboard.append([InlineKeyboardButton(category, callback_data=f"select_category_{category}")])
        keyboard.append([InlineKeyboardButton("🔙 Annuler", callback_data="cancel_add_product")])
        
        await query.message.edit_text(
            "📝 Sélectionnez la catégorie pour le nouveau produit:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SELECTING_CATEGORY

    elif query.data.startswith("select_category_"):
        # Ne traiter que si ce n'est PAS une action de suppression
        if not query.data.startswith("select_category_to_delete_"):
            category = query.data.replace("select_category_", "")
            context.user_data['temp_product_category'] = category
            
            await query.message.edit_text(
                "📝 Veuillez entrer le nom du nouveau produit:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Annuler", callback_data="cancel_add_product")
                ]])
            )
            return WAITING_PRODUCT_NAME

    elif query.data.startswith("delete_product_category_"):
        category = query.data.replace("delete_product_category_", "")
        products = CATALOG.get(category, [])
    
        keyboard = []
        for product in products:
            if isinstance(product, dict):
                keyboard.append([
                    InlineKeyboardButton(
                        product['name'],
                        callback_data=f"confirm_delete_product_{category[:10]}_{product['name'][:20]}"
                    )
                ])
        keyboard.append([InlineKeyboardButton("🔙 Annuler", callback_data="cancel_delete_product")])
    
        await query.message.edit_text(
            f"⚠️ Sélectionnez le produit à supprimer de *{category}* :",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return SELECTING_PRODUCT_TO_DELETE

    elif query.data == "delete_category":
        keyboard = []
        for category in CATALOG.keys():
            if category != 'stats':
                keyboard.append([InlineKeyboardButton(category, callback_data=f"confirm_delete_category_{category}")])
        keyboard.append([InlineKeyboardButton("🔙 Annuler", callback_data="cancel_delete_category")])
        
        await query.message.edit_text(
            "⚠️ Sélectionnez la catégorie à supprimer:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SELECTING_CATEGORY_TO_DELETE

    elif query.data.startswith("confirm_delete_category_"):
        # Ajoutez une étape de confirmation
        category = query.data.replace("confirm_delete_category_", "")
        keyboard = [
            [
                InlineKeyboardButton("✅ Oui, supprimer", callback_data=f"really_delete_category_{category}"),
                InlineKeyboardButton("❌ Non, annuler", callback_data="cancel_delete_category")
            ]
        ]
        await query.message.edit_text(
            f"⚠️ *Êtes-vous sûr de vouloir supprimer la catégorie* `{category}` *?*\n\n"
            f"Cette action supprimera également tous les produits de cette catégorie.\n"
            f"Cette action est irréversible !",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return SELECTING_CATEGORY_TO_DELETE


    elif query.data.startswith("really_delete_category_"):
        category = query.data.replace("really_delete_category_", "")
        if category in CATALOG:
            del CATALOG[category]
            save_catalog(CATALOG)
            await query.message.edit_text(
                f"✅ La catégorie *{category}* a été supprimée avec succès !",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Retour au menu", callback_data="admin")
                ]])
            )
        return CHOOSING

    elif query.data == "delete_product":
        keyboard = []
        for category in CATALOG.keys():
            if category != 'stats':
                keyboard.append([
                    InlineKeyboardButton(
                        category, 
                        callback_data=f"delete_product_category_{category}"
                    )
                ])
        keyboard.append([InlineKeyboardButton("🔙 Annuler", callback_data="cancel_delete_product")])
        
        await query.message.edit_text(
            "⚠️ Sélectionnez la catégorie du produit à supprimer:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SELECTING_CATEGORY_TO_DELETE

    elif query.data.startswith("confirm_delete_product_"):
            try:
                # Extraire la catégorie et le nom du produit
                parts = query.data.replace("confirm_delete_product_", "").split("_")
                short_category = parts[0]
                short_product = "_".join(parts[1:])  # Pour gérer les noms avec des underscores
                
                # Trouver la vraie catégorie et le vrai produit
                category = next((cat for cat in CATALOG.keys() if cat.startswith(short_category) or short_category.startswith(cat)), None)
                if category:
                    product_name = next((p['name'] for p in CATALOG[category] if p['name'].startswith(short_product) or short_product.startswith(p['name'])), None)
                    if product_name:
                        # Créer le clavier de confirmation avec les noms courts
                        keyboard = [
                            [
                                InlineKeyboardButton("✅ Oui, supprimer", 
                                    callback_data=f"really_delete_product_{category[:10]}_{product_name[:20]}"),
                                InlineKeyboardButton("❌ Non, annuler", 
                                    callback_data="cancel_delete_product")
                            ]
                        ]
                    
                        await query.message.edit_text(
                            f"⚠️ *Êtes-vous sûr de vouloir supprimer le produit* `{product_name}` *?*\n\n"
                            f"Cette action est irréversible !",
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode='Markdown'
                        )
                        return SELECTING_PRODUCT_TO_DELETE

            except Exception as e:
                print(f"Erreur lors de la confirmation de suppression: {e}")
                return await show_admin_menu(update, context)

    elif query.data.startswith("really_delete_product_"):
        try:
            parts = query.data.replace("really_delete_product_", "").split("_")
            short_category = parts[0]
            short_product = "_".join(parts[1:])

            # Trouver la vraie catégorie et le vrai produit
            category = next((cat for cat in CATALOG.keys() if cat.startswith(short_category) or short_category.startswith(cat)), None)
            if category:
                product_name = next((p['name'] for p in CATALOG[category] if p['name'].startswith(short_product) or short_product.startswith(p['name'])), None)
                if product_name:
                    CATALOG[category] = [p for p in CATALOG[category] if p['name'] != product_name]
                    save_catalog(CATALOG)
                    await query.message.edit_text(
                        f"✅ Le produit *{product_name}* a été supprimé avec succès !",
                        parse_mode='Markdown',
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔙 Retour au menu", callback_data="admin")
                        ]])
                    )
            return CHOOSING

        except Exception as e:
            print(f"Erreur lors de la suppression du produit: {e}")
            return await show_admin_menu(update, context)

    elif query.data == "toggle_access_code":
            if str(update.effective_user.id) not in ADMIN_IDS:
                await query.answer("❌ Vous n'êtes pas autorisé à modifier ce paramètre.")
                return CHOOSING
            
            is_enabled = access_manager.toggle_access_code()
            status = "activé ✅" if is_enabled else "désactivé ❌"
        
            # Afficher un message temporaire
            await query.answer(f"Le système de code d'accès a été {status}")
        
            # Rafraîchir le menu admin
            return await show_admin_menu(update, context)

    elif query.data == "edit_order_button":
            # Gérer l'affichage des configurations actuelles
            if CONFIG.get('order_url'):
                current_config = CONFIG['order_url']
                config_type = "URL"
            elif CONFIG.get('order_text'):
                current_config = CONFIG['order_text']
                config_type = "Texte"
            else:
                current_config = 'Non configuré'
                config_type = "Aucune"

            message = await query.message.edit_text(
                "🛒 Configuration du bouton Commander 🛒\n\n"
                f"<b>Configuration actuelle</b> ({config_type}):\n"
                f"{current_config}\n\n"
                "Vous pouvez :\n"
                "• Envoyer un pseudo Telegram (avec ou sans @)\n\n"
                "• Envoyer un message avec formatage HTML (<b>gras</b>, <i>italique</i>, etc)\n\n"
                "• Envoyer une URL (commençant par http:// ou https://) pour rediriger vers un site",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Annuler", callback_data="cancel_edit_order")
                ]]),
                parse_mode='HTML'  # Ajout du support HTML
            )
            context.user_data['edit_order_button_message_id'] = message.message_id
            return WAITING_ORDER_BUTTON_CONFIG

    elif query.data == "show_order_text":
        try:
            # Récupérer le message de commande configuré
            order_text = CONFIG.get('order_text', "Aucun message configuré")
        
            # Extraire la catégorie du message précédent
            category = None
            for markup_row in query.message.reply_markup.inline_keyboard:
                for button in markup_row:
                    if button.callback_data and button.callback_data.startswith("view_"):
                        category = button.callback_data.replace("view_", "")
                        break
                if category:
                    break
        
            keyboard = [[
                InlineKeyboardButton("🔙 Retour aux produits", callback_data=f"view_{category}")
            ]]
        
            # Modifier le message existant au lieu d'en créer un nouveau
            # Utiliser parse_mode='HTML' au lieu de 'Markdown'
            await query.message.edit_text(
                text=order_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            return CHOOSING
        
        except Exception as e:
            print(f"Erreur lors de l'affichage du message: {e}")
            await query.answer("Une erreur est survenue lors de l'affichage du message", show_alert=True)
            return CHOOSING


    elif query.data == "edit_welcome":
            current_message = CONFIG.get('welcome_message', "Message non configuré")
        
            message = await query.message.edit_text(
                "✏️ Configuration du message d'accueil\n\n"
                f"Message actuel :\n{current_message}\n\n"
                "Envoyez le nouveau message d'accueil.\n"
                "Vous pouvez utiliser le formatage HTML :\n"
                "• <b>texte</b> pour le gras\n"
                "• <i>texte</i> pour l'italique\n"
                "• <u>texte</u> pour le souligné",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Annuler", callback_data="cancel_edit_welcome")
                ]]),
                parse_mode='HTML'
            )
            context.user_data['edit_welcome_message_id'] = message.message_id
            return WAITING_WELCOME_MESSAGE

    elif query.data == "show_stats":
        # Configuration du fuseau horaire Paris
        paris_tz = pytz.timezone('Europe/Paris')
        utc_now = datetime.utcnow()
        paris_now = utc_now.replace(tzinfo=pytz.UTC).astimezone(paris_tz)

        # Initialisation des stats si nécessaire
        if 'stats' not in CATALOG:
            CATALOG['stats'] = {
                "total_views": 0,
                "category_views": {},
                "product_views": {},
                "last_updated": paris_now.strftime("%H:%M:%S"),
                "last_reset": paris_now.strftime("%Y-%m-%d")
            }
    
        # Nettoyer les stats avant l'affichage
        clean_stats()
    
        stats = CATALOG['stats']
        text = "📊 *Statistiques du catalogue*\n\n"
        text += f"👥 Vues totales: {stats.get('total_views', 0)}\n"
    
        # Conversion de l'heure en fuseau horaire Paris
        last_updated = stats.get('last_updated', 'Jamais')
        if last_updated != 'Jamais':
            try:
                if len(last_updated) > 8:  # Si format complet
                    dt = datetime.strptime(last_updated, "%Y-%m-%d %H:%M:%S")
                else:  # Si format HH:MM:SS
                    today = paris_now.strftime("%Y-%m-%d")
                    dt = datetime.strptime(f"{today} {last_updated}", "%Y-%m-%d %H:%M:%S")
            
                # Convertir en timezone Paris
                dt = dt.replace(tzinfo=pytz.UTC).astimezone(paris_tz)
                last_updated = dt.strftime("%H:%M:%S")
            except Exception as e:
                print(f"Erreur conversion heure: {e}")
            
        text += f"🕒 Dernière mise à jour: {last_updated}\n"
    
        if 'last_reset' in stats:
            text += f"🔄 Dernière réinitialisation: {stats.get('last_reset', 'Jamais')}\n"
        text += "\n"
    
        # Le reste du code reste identique
        text += "📈 *Vues par catégorie:*\n"
        category_views = stats.get('category_views', {})
        if category_views:
            sorted_categories = sorted(category_views.items(), key=lambda x: x[1], reverse=True)
            for category, views in sorted_categories:
                if category in CATALOG:
                    text += f"- {category}: {views} vues\n"
        else:
            text += "Aucune vue enregistrée.\n"

        text += "\n━━━━━━━━━━━━━━━\n\n"
    
        text += "🔥 *Produits les plus populaires:*\n"
        product_views = stats.get('product_views', {})
        if product_views:
            all_products = []
            for category, products in product_views.items():
                if category in CATALOG:
                    existing_products = [p['name'] for p in CATALOG[category]]
                    for product_name, views in products.items():
                        if product_name in existing_products:
                            all_products.append((category, product_name, views))
        
            sorted_products = sorted(all_products, key=lambda x: x[2], reverse=True)[:5]
            for category, product_name, views in sorted_products:
                text += f"- {product_name} ({category}): {views} vues\n"
        else:
            text += "Aucune vue enregistrée sur les produits.\n"
    
        keyboard = [
            [InlineKeyboardButton("🔄 Réinitialiser les statistiques", callback_data="confirm_reset_stats")],
            [InlineKeyboardButton("🔙 Retour", callback_data="admin")]
        ]
    
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

    elif query.data == "edit_contact":
            # Gérer l'affichage de la configuration actuelle
            if CONFIG.get('contact_username'):
                current_config = f"@{CONFIG['contact_username']}"
                config_type = "Pseudo Telegram"
            elif CONFIG.get('contact_url'):  # Ajout d'une nouvelle option pour l'URL
                current_config = CONFIG['contact_url']
                config_type = "URL"
            else:
                current_config = 'Non configuré'
                config_type = "Aucune"

            message = await query.message.edit_text(
                "📱 Configuration du contact\n\n"
                f"Configuration actuelle ({config_type}):\n"
                f"{current_config}\n\n"
                "Vous pouvez :\n"
                "• Envoyer un pseudo Telegram (avec ou sans @)\n"
                "• Envoyer une URL (commençant par http:// ou https://) pour rediriger vers un site",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Annuler", callback_data="cancel_edit_contact")
                ]]),
                parse_mode='HTML'
            )
            context.user_data['edit_contact_message_id'] = query.message.message_id
            return WAITING_CONTACT_USERNAME

    elif query.data in ["cancel_add_category", "cancel_add_product", "cancel_delete_category", 
                        "cancel_delete_product", "cancel_edit_contact", "cancel_edit_order", "cancel_edit_welcome"]:
        return await show_admin_menu(update, context)

    elif query.data == "back_to_categories":
        if 'category_message_id' in context.user_data:
            try:
                await context.bot.edit_message_text(
                    chat_id=query.message.chat_id,
                    message_id=context.user_data['category_message_id'],
                    text=context.user_data['category_message_text'],
                    reply_markup=InlineKeyboardMarkup(context.user_data['category_message_reply_markup']),
                    parse_mode='Markdown'
                )
            except Exception as e:
                print(f"Erreur lors de la mise à jour du message des catégories: {e}")
        else:
            # Si le message n'existe pas, recréez-le
            keyboard = []
            for category in CATALOG.keys():
                if category != 'stats':
                    keyboard.append([InlineKeyboardButton(category, callback_data=f"view_{category}")])

            keyboard.append([InlineKeyboardButton("🔙 Retour à l'accueil", callback_data="back_to_home")])

            await query.edit_message_text(
                "📋 *Menu*\n\n"
                "Choisissez une catégorie pour voir les produits :",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )

    elif query.data == "skip_media":
        category = context.user_data.get('temp_product_category')
        if category:
            new_product = {
                'name': context.user_data.get('temp_product_name'),
                'price': context.user_data.get('temp_product_price'),
                'description': context.user_data.get('temp_product_description')
            }
            
            if category not in CATALOG:
                CATALOG[category] = []
            CATALOG[category].append(new_product)
            save_catalog(CATALOG)
            
            context.user_data.clear()
            return await show_admin_menu(update, context)

    elif query.data.startswith("product_"):
                _, category, product_name = query.data.split("_", 2)
                
                # Trouver la vraie catégorie et le vrai produit
                real_category = next((cat for cat in CATALOG.keys() if cat.startswith(category) or category.startswith(cat)), None)
                if real_category:
                    product = next((p for p in CATALOG[real_category] if p['name'].startswith(product_name) or product_name.startswith(p['name'])), None)
                    if product:
                        category = real_category  # Utiliser la vraie catégorie pour la suite
                        caption = f"📱 <b>{product['name']}</b>\n\n"
                        caption += f"💰 <b>Prix:</b>\n{product['price']}\n\n"
                        caption += f"📝 <b>Description:</b>\n{product['description']}"

                        keyboard = [[
                            InlineKeyboardButton("🔙 Retour à la catégorie", callback_data=f"view_{category}"),
                            InlineKeyboardButton(
                                "🛒 Commander",
                                **({"url": CONFIG['order_url']} if CONFIG.get('order_url') 
                                   else {"callback_data": "show_order_text"})
                            )
                        ]]
                        if 'media' in product and product['media']:
                            media_list = product['media']
                            media_list = sorted(media_list, key=lambda x: x.get('order_index', 0))
                            total_media = len(media_list)
                            context.user_data['current_media_index'] = 0
                            current_media = media_list[0]

                            if total_media > 1:
                                keyboard.insert(0, [
                                    InlineKeyboardButton("⬅️ Précédent", callback_data=f"prev_media_{category[:10]}_{product['name'][:20]}"),
                                    InlineKeyboardButton("➡️ Suivant", callback_data=f"next_media_{category[:10]}_{product['name'][:20]}")
                                ])

                            await query.message.delete()

                            if current_media['media_type'] == 'photo':
                                message = await context.bot.send_photo(
                                    chat_id=query.message.chat_id,
                                    photo=current_media['media_id'],
                                    caption=caption,
                                    reply_markup=InlineKeyboardMarkup(keyboard),
                                    parse_mode='HTML'  # Changé en HTML au lieu de Markdown
                                )
                            else:
                                message = await context.bot.send_video(
                                    chat_id=query.message.chat_id,
                                    video=current_media['media_id'],
                                    caption=caption,
                                    reply_markup=InlineKeyboardMarkup(keyboard),
                                    parse_mode='HTML'  # Changé en HTML au lieu de Markdown
                                )
                            context.user_data['last_product_message_id'] = message.message_id
                        else:
                            await query.message.edit_text(
                                text=caption,
                                reply_markup=InlineKeyboardMarkup(keyboard),
                                parse_mode='HTML'  # Changé en HTML au lieu de Markdown
                            )
                        if product:
                            # Incrémenter les stats du produit
                            if 'stats' not in CATALOG:
                                CATALOG['stats'] = {...}  # même initialisation que ci-dessus
            
                            if 'product_views' not in CATALOG['stats']:
                                CATALOG['stats']['product_views'] = {}
                            if category not in CATALOG['stats']['product_views']:
                                CATALOG['stats']['product_views'][category] = {}
                            if product['name'] not in CATALOG['stats']['product_views'][category]:
                                CATALOG['stats']['product_views'][category][product['name']] = 0
            
                            CATALOG['stats']['product_views'][category][product['name']] += 1
                            CATALOG['stats']['total_views'] += 1
                            CATALOG['stats']['last_updated'] = datetime.now(paris_tz).strftime("%H:%M:%S")
                            save_catalog(CATALOG)

    elif query.data.startswith("view_"):
            category = query.data.replace("view_", "")
            if category in CATALOG:
                # Initialisation des stats si nécessaire
                if 'stats' not in CATALOG:
                    CATALOG['stats'] = {
                        "total_views": 0,
                        "category_views": {},
                        "product_views": {},
                        "last_updated": datetime.now(paris_tz).strftime("%H:%M:%S")
                    }

                if 'category_views' not in CATALOG['stats']:
                    CATALOG['stats']['category_views'] = {}
    
                if category not in CATALOG['stats']['category_views']:
                    CATALOG['stats']['category_views'][category] = 0
    
                # Mettre à jour les statistiques
                CATALOG['stats']['category_views'][category] += 1
                CATALOG['stats']['total_views'] += 1
                CATALOG['stats']['last_updated'] = datetime.now(paris_tz).strftime("%H:%M:%S")
                save_catalog(CATALOG)

                products = CATALOG[category]
                # Afficher la liste des produits
                text = f"*{category}*\n\n"
                keyboard = []
                for product in products:
                    keyboard.append([InlineKeyboardButton(
                        product['name'],
                        callback_data=f"product_{category[:10]}_{product['name'][:20]}"
                    )])

                keyboard.append([InlineKeyboardButton("🔙 Retour au menu", callback_data="show_categories")])

                try:
                    # Suppression du dernier message de produit (photo ou vidéo) si existe
                    if 'last_product_message_id' in context.user_data:
                        try:
                            await context.bot.delete_message(
                                chat_id=query.message.chat_id,
                                message_id=context.user_data['last_product_message_id']
                            )
                            del context.user_data['last_product_message_id']
                        except:
                            pass

                    print(f"Texte du message : {text}")
                    print(f"Clavier : {keyboard}")

                    # Éditer le message existant au lieu de le supprimer et recréer
                    await query.message.edit_text(
                        text=text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode='Markdown'
                    )
                
                    context.user_data['category_message_id'] = query.message.message_id
                    context.user_data['category_message_text'] = text
                    context.user_data['category_message_reply_markup'] = keyboard

                except Exception as e:
                    print(f"Erreur lors de la mise à jour du message des produits: {e}")
                    # Si l'édition échoue, on crée un nouveau message
                    message = await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode='Markdown'
                    )
                    context.user_data['category_message_id'] = message.message_id

                # Mettre à jour les stats des produits seulement s'il y en a
                if products:
                    if 'stats' not in CATALOG:
                        CATALOG['stats'] = {
                            "total_views": 0,
                            "category_views": {},
                            "product_views": {},
                            "last_updated": datetime.now(paris_tz).strftime("%H:%M:%S"),
                            "last_reset": datetime.now(paris_tz).strftime("%Y-%m-%d")
                        }

                    if 'product_views' not in CATALOG['stats']:
                        CATALOG['stats']['product_views'] = {}
                    if category not in CATALOG['stats']['product_views']:
                        CATALOG['stats']['product_views'][category] = {}

                    # Mettre à jour les stats pour chaque produit dans la catégorie
                    for product in products:
                        if product['name'] not in CATALOG['stats']['product_views'][category]:
                            CATALOG['stats']['product_views'][category][product['name']] = 0
                        CATALOG['stats']['product_views'][category][product['name']] += 1

                    save_catalog(CATALOG)

    elif query.data.startswith(("next_media_", "prev_media_")):
            try:
                _, direction, short_category, short_product = query.data.split("_", 3)
            
                # Trouver la vraie catégorie
                category = next((cat for cat in CATALOG.keys() if cat.startswith(short_category) or short_category.startswith(cat)), None)
                if category:
                    # Trouver le vrai produit en utilisant le début du nom
                    product = next((p for p in CATALOG[category] if p['name'].startswith(short_product) or short_product.startswith(p['name'])), None)

                    if product and 'media' in product:
                        media_list = sorted(product['media'], key=lambda x: x.get('order_index', 0))
                        total_media = len(media_list)
                        current_index = context.user_data.get('current_media_index', 0)

                        # Le reste de votre code reste identique
                        if direction == "next":
                            current_index = current_index + 1
                            if current_index >= total_media:
                                current_index = 0
                        else:  # prev
                            current_index = current_index - 1
                            if current_index < 0:
                                current_index = total_media - 1

                        context.user_data['current_media_index'] = current_index
                        current_media = media_list[current_index]

                        caption = f"📱 *{product['name']}*\n\n"
                        caption += f"💰 *Prix:*\n{product['price']}\n\n"
                        caption += f"📝 *Description:*\n{product['description']}"

                        keyboard = []
                        if total_media > 1:
                            keyboard.append([
                                InlineKeyboardButton("⬅️ Précédent", callback_data=f"prev_media_{short_category}_{short_product}"),
                                InlineKeyboardButton("➡️ Suivant", callback_data=f"next_media_{short_category}_{short_product}")
                            ])
                        keyboard.append([
                            InlineKeyboardButton("🔙 Retour à la catégorie", callback_data=f"view_{category}"),
                            InlineKeyboardButton(
                                "🛒 Commander",
                                **({"url": CONFIG.get('order_url')} if CONFIG.get('order_url') else {"callback_data": "show_order_text"})
                            )
                        ])

                        try:
                            await query.message.delete()
                        except Exception as e:
                            print(f"Erreur lors de la suppression du message: {e}")

                        if current_media['media_type'] == 'photo':
                            message = await context.bot.send_photo(
                                chat_id=query.message.chat_id,
                                photo=current_media['media_id'],
                                caption=caption,
                                reply_markup=InlineKeyboardMarkup(keyboard),
                                parse_mode='Markdown'
                            )
                        else:  # video
                            message = await context.bot.send_video(
                                chat_id=query.message.chat_id,
                                video=current_media['media_id'],
                                caption=caption,
                                reply_markup=InlineKeyboardMarkup(keyboard),
                                parse_mode='Markdown'
                            )
                        context.user_data['last_product_message_id'] = message.message_id

            except Exception as e:
                print(f"Erreur lors de la navigation des médias: {e}")
                await query.answer("Une erreur est survenue")

    elif query.data == "edit_product":
        keyboard = []
        for category in CATALOG.keys():
            if category != 'stats':
                keyboard.append([
                    InlineKeyboardButton(
                        category, 
                        callback_data=f"editcat_{category}"  # Raccourci ici
                    )
                ])
        keyboard.append([InlineKeyboardButton("🔙 Annuler", callback_data="cancel_edit")])
        
        await query.message.edit_text(
            "✏️ Sélectionnez la catégorie du produit à modifier:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SELECTING_CATEGORY

    elif query.data.startswith("editcat_"):  # Nouveau gestionnaire avec nom plus court
        category = query.data.replace("editcat_", "")
        products = CATALOG.get(category, [])
        
        keyboard = []
        for product in products:
            if isinstance(product, dict):
                # Créer un callback_data plus court
                callback_data = f"editp_{category[:10]}_{product['name'][:20]}"
                keyboard.append([
                    InlineKeyboardButton(product['name'], callback_data=callback_data)
                ])
        keyboard.append([InlineKeyboardButton("🔙 Annuler", callback_data="cancel_edit")])
        
        await query.message.edit_text(
            f"✏️ Sélectionnez le produit à modifier dans {category}:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SELECTING_PRODUCT_TO_EDIT

    elif query.data.startswith("editp_"):
        try:
            _, short_category, short_product = query.data.split("_", 2)
            
            # Trouver la vraie catégorie et le vrai produit
            category = next((cat for cat in CATALOG.keys() if cat.startswith(short_category) or short_category.startswith(cat)), None)
            if category:
                product_name = next((p['name'] for p in CATALOG[category] if p['name'].startswith(short_product) or short_product.startswith(p['name'])), None)
                if product_name:
                    context.user_data['editing_category'] = category
                    context.user_data['editing_product'] = product_name

                    keyboard = [
                        [InlineKeyboardButton("📝 Nom", callback_data="edit_name")],
                        [InlineKeyboardButton("💰 Prix", callback_data="edit_price")],
                        [InlineKeyboardButton("📝 Description", callback_data="edit_desc")],
                        [InlineKeyboardButton("📸 Médias", callback_data="edit_media")],  # Nouvelle option
                        [InlineKeyboardButton("🔙 Annuler", callback_data="cancel_edit")]
                    ]

                    await query.message.edit_text(
                        f"✏️ Que souhaitez-vous modifier pour *{product_name}* ?\n"
                        "Sélectionnez un champ à modifier:",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode='Markdown'
                    )
                    return EDITING_PRODUCT_FIELD
            
            return await show_admin_menu(update, context)
        except Exception as e:
            print(f"Erreur dans editp_: {e}")
            return await show_admin_menu(update, context)

    elif query.data in ["edit_name", "edit_price", "edit_desc", "edit_media"]:  # Ajout de edit_media
        field_mapping = {
            "edit_name": "name",
            "edit_price": "price",
            "edit_desc": "description",
            "edit_media": "media"
        }
        field = field_mapping[query.data]
        context.user_data['editing_field'] = field
    
        category = context.user_data.get('editing_category')
        product_name = context.user_data.get('editing_product')
    
        product = next((p for p in CATALOG[category] if p['name'] == product_name), None)
    
        if product:
            if field == 'media':
                keyboard = [
                    [InlineKeyboardButton("❌ Supprimer tous les médias", callback_data="clear_media")],
                    [InlineKeyboardButton("🔙 Annuler", callback_data="cancel_edit")]
                ]
            
                current_media_count = len(product.get('media', []))
                await query.message.edit_text(
                    f"📸 Gestion des médias du produit\n\n"
                    f"Médias actuels : {current_media_count}\n\n"
                    "Pour ajouter de nouveaux médias, envoyez des photos ou vidéos.\n"
                    "Vous pouvez en envoyer plusieurs à la suite.\n"
                    "Pour terminer, cliquez sur Terminé.\n\n"
                    "Note : Les nouveaux médias remplaceront les anciens.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Terminé", callback_data="finish_media")],
                        *keyboard
                    ])
                )
                return WAITING_PRODUCT_MEDIA
            else:
                field_names = {
                    'name': 'nom',
                    'price': 'prix',
                    'description': 'description'
                }
                await query.message.edit_text(
                    f"✏️ Modification du {field_names.get(field, field)}\n"
                    f"Valeur actuelle : {current_value}\n\n"
                    "Envoyez la nouvelle valeur :",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Annuler", callback_data="cancel_edit")
                    ]])
                )
                return WAITING_NEW_VALUE

    elif query.data == "clear_media":
        category = context.user_data.get('editing_category')
        product_name = context.user_data.get('editing_product')
    
        if category and product_name:
            for product in CATALOG[category]:
                if product['name'] == product_name:
                    if 'media' in product:
                        del product['media']
                    save_catalog(CATALOG)
                
                    await query.answer("✅ Tous les médias ont été supprimés")
                    return await show_admin_menu(update, context)

    elif query.data == "finish_media":
        category = context.user_data.get('editing_category')
        product_name = context.user_data.get('editing_product')
    
        if category and product_name:
            for product in CATALOG[category]:
                if product['name'] == product_name:
                    new_media = context.user_data.get('temp_product_media', [])
                    if new_media:
                        product['media'] = new_media
                        save_catalog(CATALOG)
                
            # Nettoyer les données temporaires
            if 'temp_product_media' in context.user_data:
                del context.user_data['temp_product_media']
        
            await query.answer("✅ Médias mis à jour avec succès")
            return await show_admin_menu(update, context)

    elif query.data == "cancel_edit":
        return await show_admin_menu(update, context)

    elif query.data == "confirm_reset_stats":
        # Réinitialiser les statistiques
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        CATALOG['stats'] = {
            "total_views": 0,
            "category_views": {},
            "product_views": {},
            "last_updated": now.split(" ")[1],  # Juste l'heure
            "last_reset": now.split(" ")[0]  # Juste la date
        }
        save_catalog(CATALOG)
        
        # Afficher un message de confirmation
        keyboard = [[InlineKeyboardButton("🔙 Retour au menu", callback_data="admin")]]
        await query.message.edit_text(
            "✅ *Les statistiques ont été réinitialisées avec succès!*\n\n"
            f"Date de réinitialisation : {CATALOG['stats']['last_reset']}\n\n"
            "Toutes les statistiques sont maintenant à zéro.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
               
    elif query.data == "show_categories":
        keyboard = []
        # Créer uniquement les boutons de catégories
        for category in CATALOG.keys():
            if category != 'stats':
                keyboard.append([InlineKeyboardButton(category, callback_data=f"view_{category}")])

        # Ajouter uniquement le bouton retour à l'accueil
        keyboard.append([InlineKeyboardButton("🔙 Retour à l'accueil", callback_data="back_to_home")])

        try:
            message = await query.edit_message_text(
                "📋 *Menu*\n\n"
                "Choisissez une catégorie pour voir les produits :",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            context.user_data['menu_message_id'] = message.message_id
        except Exception as e:
            print(f"Erreur lors de la mise à jour du message des catégories: {e}")
            # Si la mise à jour échoue, recréez le message
            message = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="📋 *Menu*\n\n"
                     "Choisissez une catégorie pour voir les produits :",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            context.user_data['menu_message_id'] = message.message_id

    elif query.data == "back_to_home":  # Ajout de cette condition ici
            chat_id = update.effective_chat.id

            # Définir le texte de bienvenue ici, avant les boutons
            welcome_text = CONFIG.get('welcome_message', 
                "🌿 <b>Bienvenue sur votre bot !</b> 🌿\n\n"
                "<b>Pour changer ce message d accueil, rendez vous dans l onglet admin.</b>\n"
                "📋 Cliquez sur MENU pour voir les catégories"
            )

            # Nouveau clavier simplifié pour l'accueil
            keyboard = [
                [InlineKeyboardButton("📋 MENU", callback_data="show_categories")]
            ]

            # Ajouter le bouton admin si l'utilisateur est administrateur
            if str(update.effective_user.id) in ADMIN_IDS:
                keyboard.append([InlineKeyboardButton("🔧 Menu Admin", callback_data="admin")])

            # Configurer le bouton de contact en fonction du type (URL ou username)
            contact_button = None
            if CONFIG.get('contact_url'):
                contact_button = InlineKeyboardButton("📞 Contact", url=CONFIG['contact_url'])
            elif CONFIG.get('contact_username'):
                contact_button = InlineKeyboardButton("📞 Contact Telegram", url=f"https://t.me/{CONFIG['contact_username']}")

            # Ajouter les boutons de contact et canaux
            if contact_button:
                keyboard.extend([
                    [
                        contact_button,
                        InlineKeyboardButton("💭 Tchat telegram", url="https://t.me/+YsJIgYjY8_cyYzBk"),
                    ],
                    [InlineKeyboardButton("🥔 Canal potato", url="https://doudlj.org/joinchat/QwqUM5gH7Q8VqO3SnS4YwA")]
                ])
            else:
                keyboard.extend([
                    [InlineKeyboardButton("💭 Tchat telegram", url="https://t.me/+YsJIgYjY8_cyYzBk")],
                    [InlineKeyboardButton("🥔 Canal potato", url="https://doudlj.org/joinchat/QwqUM5gH7Q8VqO3SnS4YwA")]
                ])

            await query.message.edit_text(
                text=welcome_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'  
            )
            return CHOOSING

async def get_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler temporaire pour obtenir le file_id de l'image banner"""
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        CONFIG['banner_image'] = file_id
        # Sauvegarder dans config.json
        with open('config.json', 'w', encoding='utf-8') as f:
            json.dump(CONFIG, f, indent=4)
        await update.message.reply_text(
            f"✅ Image banner enregistrée!\nFile ID: {file_id}"
        )


    # Récupérer le chat_id et le message
    if update.callback_query:
        chat_id = update.callback_query.message.chat_id
    else:
        chat_id = update.effective_chat.id

    # Nouveau clavier simplifié pour l'accueil
    keyboard = [
        [InlineKeyboardButton("📋 MENU", callback_data="show_categories")]
    ]

    # Ajouter le bouton admin si l'utilisateur est administrateur
    if str(update.effective_user.id) in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("🔧 Menu Admin", callback_data="admin")])

    # Configurer le bouton de contact en fonction du type (URL ou username)
    contact_button = None
    if CONFIG.get('contact_url'):
        contact_button = InlineKeyboardButton("📞 Contact", url=CONFIG['contact_url'])
    elif CONFIG.get('contact_username'):
        contact_button = InlineKeyboardButton("📞 Contact Telegram", url=f"https://t.me/{CONFIG['contact_username']}")

    # Ajouter les boutons de contact et canaux
    if contact_button:
        keyboard.extend([
            [
                contact_button,
                InlineKeyboardButton("💭 Tchat telegram", url="https://t.me/+YsJIgYjY8_cyYzBk"),
            ],
            [InlineKeyboardButton("🥔 Canal potato", url="https://doudlj.org/joinchat/QwqUM5gH7Q8VqO3SnS4YwA")]
        ])
    else:
        keyboard.extend([
            [InlineKeyboardButton("💭 Tchat telegram", url="https://t.me/+YsJIgYjY8_cyYzBk")],
            [InlineKeyboardButton("🥔 Canal potato", url="https://doudlj.org/joinchat/QwqUM5gH7Q8VqO3SnS4YwA")]
        ])

        welcome_text = CONFIG.get('welcome_message', 
            "🌿 <b>Bienvenue votre bot !</b> 🌿\n\n"
            "<b>Pour changer ce message d accueil, rendez vous dans l onglet admin.</b>\n"
            "📋 Cliquez sur MENU pour voir les catégories"
        )

    try:
        if update.callback_query:
            # Si c'est un callback, on édite le message existant
            await update.callback_query.edit_message_text(
                text=welcome_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
        else:
            # Sinon, on envoie un nouveau message
            menu_message = await context.bot.send_message(
                chat_id=chat_id,
                text=welcome_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            context.user_data['menu_message_id'] = menu_message.message_id

    except Exception as e:
        print(f"Erreur lors du retour à l'accueil: {e}")
        # En cas d'erreur, on essaie d'envoyer un nouveau message
        try:
            menu_message = await context.bot.send_message(
                chat_id=chat_id,
                text=welcome_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            context.user_data['menu_message_id'] = menu_message.message_id
        except Exception as e:
            print(f"Erreur critique lors du retour à l'accueil: {e}")

    return CHOOSING

def main():
    """Fonction principale du bot"""
    try:
        # Créer l'application
        global admin_features
        application = Application.builder().token(TOKEN).build()
        admin_features = AdminFeatures()

        # Initialiser l'access manager
        global access_manager
        access_manager = AccessManager()

        # Gestionnaire de conversation principal
        conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('admin', admin),
            CallbackQueryHandler(handle_normal_buttons, pattern='^(show_categories|back_to_home|admin)$'),
        ],
        states={
            CHOOSING: [
                CallbackQueryHandler(handle_normal_buttons),
            ],
            WAITING_CATEGORY_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_category_name),
                CallbackQueryHandler(handle_normal_buttons),
            ],
            WAITING_PRODUCT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_name),
                CallbackQueryHandler(handle_normal_buttons),
            ],
            WAITING_PRODUCT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_price),
                CallbackQueryHandler(handle_normal_buttons),
            ],
            WAITING_PRODUCT_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_description),
                CallbackQueryHandler(handle_normal_buttons),
            ],
            WAITING_PRODUCT_MEDIA: [
                MessageHandler(filters.PHOTO | filters.VIDEO, handle_product_media),
                CallbackQueryHandler(handle_normal_buttons),
            ],
            SELECTING_CATEGORY: [
                CallbackQueryHandler(handle_normal_buttons),
            ],
            SELECTING_CATEGORY_TO_DELETE: [
                CallbackQueryHandler(handle_normal_buttons),
            ],
            SELECTING_PRODUCT_TO_DELETE: [
                CallbackQueryHandler(handle_normal_buttons),
            ],
            WAITING_CONTACT_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_contact_username),
                CallbackQueryHandler(handle_normal_buttons),
            ],
            SELECTING_PRODUCT_TO_EDIT: [
                CallbackQueryHandler(handle_normal_buttons),
            ],
            EDITING_PRODUCT_FIELD: [
                CallbackQueryHandler(handle_normal_buttons),
            ],
            WAITING_NEW_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_value),
                CallbackQueryHandler(handle_normal_buttons),
            ],
            WAITING_BANNER_IMAGE: [
                MessageHandler(filters.PHOTO, handle_banner_image),
                CallbackQueryHandler(handle_normal_buttons),
            ],
            WAITING_WELCOME_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_welcome_message),
                CallbackQueryHandler(handle_normal_buttons)
            ],
            WAITING_ORDER_BUTTON_CONFIG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_order_button_config),
                CallbackQueryHandler(handle_normal_buttons),
            ],
            WAITING_PRODUCT_MEDIA: [
                MessageHandler(filters.PHOTO | filters.VIDEO, handle_product_media),
                CallbackQueryHandler(finish_product_media, pattern="^finish_media$"),
                CallbackQueryHandler(handle_normal_buttons),
            ],
            WAITING_FOR_ACCESS_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_access_code),
                CallbackQueryHandler(start, pattern="^cancel_access$"),
            ],
            WAITING_BROADCAST_MESSAGE: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND,
                    admin_features.send_broadcast_message
                ),
                CallbackQueryHandler(handle_normal_buttons)
            ],
            
        },
        fallbacks=[
            CommandHandler('start', start),
            CommandHandler('admin', admin),
        ],
        name="main_conversation",
        persistent=False,
    )

        application.add_handler(CommandHandler("gencode", admin_generate_code))
        application.add_handler(CommandHandler("listcodes", admin_list_codes))

        application.add_handler(conv_handler)
        # Démarrer le bot
        print("Bot démarré...")
        application.run_polling()

    except Exception as e:
        print(f"Erreur lors du démarrage du bot: {e}")

if __name__ == '__main__':
    main()
