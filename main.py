import logging
import sqlite3
from datetime import datetime
from typing import Dict, Set
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

# Configuration du logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Stockage des parties en cours
dice_games: Dict[str, Dict] = {}
user_games: Dict[int, str] = {} 
ADMIN_IDS = {5277718388, 5909979625}  # Un seul ensemble avec les deux IDs
DICE_TOPIC_ID = 15 

active_credit_drops: Dict[str, Dict] = {}

def escape_markdown(text: str) -> str:
    """Échappe les caractères spéciaux Markdown dans un texte"""
    return text.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`').replace('-', '\\-')

class DatabaseManager:
    def __init__(self, db_file="blackjack.db"):
        self.db_file = db_file
        self.conn = sqlite3.connect(db_file)
        self.cursor = self.conn.cursor()

    def get_balance(self, user_id: int) -> int:
        """Récupère le solde d'un utilisateur"""
        try:
            self.cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
            result = self.cursor.fetchone()
            return result[0] if result else 0
        except Exception as e:
            print(f"Erreur dans get_balance: {e}")
            return 0

    def set_balance(self, user_id: int, amount: int) -> bool:
        """Définit le solde d'un utilisateur"""
        try:
            self.cursor.execute('UPDATE users SET balance = ? WHERE user_id = ?', 
                              (amount, user_id))
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Erreur dans set_balance: {e}")
            self.conn.rollback()
            return False

    def add_balance(self, user_id: int, amount: int) -> bool:
        """Ajoute (ou soustrait) un montant au solde d'un utilisateur"""
        try:
            current_balance = self.get_balance(user_id)
            new_balance = current_balance + amount
            return self.set_balance(user_id, new_balance)
        except Exception as e:
            print(f"Erreur dans add_balance: {e}")
            return False

    def user_exists(self, user_id: int) -> bool:
        """Vérifie si un utilisateur existe"""
        try:
            self.cursor.execute('SELECT 1 FROM users WHERE user_id = ?', (user_id,))
            return bool(self.cursor.fetchone())
        except Exception as e:
            print(f"Erreur dans user_exists: {e}")
            return False

# Initialisation de la base de données
db = DatabaseManager()

async def dropcredits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande admin pour créer un drop de crédits"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id if update.message else None
    
    # Vérifier si l'utilisateur est un admin
    if user.id not in ADMIN_IDS:
        await update.message.reply_text(
            "❌ Cette commande est réservée aux administrateurs.",
            message_thread_id=thread_id
        )
        await update.message.delete()
        return
        
    try:
        # Vérification des arguments
        if len(context.args) != 1:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Usage incorrect.\n"
                     "Usage: `/dropcredits montant`\n"
                     "Exemple: `/dropcredits 1000`",
                parse_mode=ParseMode.MARKDOWN,
                message_thread_id=thread_id
            )
            await update.message.delete()
            return
            
        # Validation du montant
        try:
            amount = int(context.args[0])
        except ValueError:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Le montant doit être un nombre valide.",
                message_thread_id=thread_id
            )
            await update.message.delete()
            return
        
        if amount <= 0:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Le montant doit être positif.",
                message_thread_id=thread_id
            )
            await update.message.delete()
            return
            
        if chat_id in active_credit_drops:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Une distribution de crédits est déjà active dans ce chat.",
                message_thread_id=thread_id
            )
            await update.message.delete()
            return
            
        # Création du bouton
        keyboard = [[InlineKeyboardButton("🎁 Récupérer les crédits!", callback_data=f"claim_credits_{amount}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Envoi du message avec le bouton
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎁 *Distribution de crédits*\n"
                 f"├ Montant: {amount} 💵\n"
                 f"└ Premier arrivé, premier servi!\n\n"
                 f"_Cliquez sur le bouton pour récupérer les crédits_",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
            message_thread_id=thread_id
        )
        
        # Supprimer la commande après avoir envoyé le message
        await update.message.delete()
        
        # Enregistrement de la distribution active
        drop_id = f"{chat_id}_{message.message_id}"
        active_credit_drops[drop_id] = {
            'amount': amount,
            'message_id': message.message_id,
            'claimed': False
        }
        
    except Exception as e:
        print(f"Error in dropcredits: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Une erreur s'est produite.",
            message_thread_id=thread_id
        )
        await update.message.delete()

async def handle_credit_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère la réclamation des crédits"""
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    
    try:
        drop_id = f"{chat_id}_{query.message.message_id}"
        if drop_id not in active_credit_drops:
            await query.answer("❌ Cette distribution n'est plus active!", show_alert=True)
            return
            
        drop = active_credit_drops[drop_id]
        
        if drop['claimed']:
            await query.answer("❌ Ces crédits ont déjà été réclamés!", show_alert=True)
            return
            
        amount = int(query.data.split('_')[2])
        
        # Marquer comme réclamé
        drop['claimed'] = True
        
        # Ajouter les crédits
        if db.add_balance(user.id, amount):
            new_balance = db.get_balance(user.id)
            
            # Mettre à jour le message
            await query.message.edit_text(
                f"🎁 *Distribution de crédits terminée*\n"
                f"├ Gagnant: {user.username if user.username else user.first_name}\n"
                f"├ Montant: +{amount} 💵\n"
                f"└ Nouveau solde: {new_balance} 💵",
                parse_mode=ParseMode.MARKDOWN
            )
            
            await query.answer(f"✅ Vous avez reçu {amount} 💵!", show_alert=True)
            
        else:
            await query.answer("❌ Une erreur s'est produite lors de l'attribution des crédits.", show_alert=True)
            
        # Supprimer le drop
        del active_credit_drops[drop_id]
        
    except Exception as e:
        print(f"Error in handle_credit_claim: {e}")
        await query.answer("❌ Une erreur s'est produite.", show_alert=True)

async def bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id if update.message else None    

    if DICE_TOPIC_ID and thread_id != DICE_TOPIC_ID:
        await update.message.reply_text(
            "❌ Les parties de dés ne sont autorisées que dans le topic dédié!",
            message_thread_id=thread_id
        )
        return
    
    # Vérifier si l'utilisateur a déjà une partie en cours
    if user.id in user_games:
        await update.message.reply_text("❌ Vous avez déjà une partie en cours!")
        return

    try:
        # Vérification des arguments
        if len(context.args) != 1:
            await update.message.reply_text(
                "❌ Usage incorrect.\n"
                "Usage: `/bet montant`\n"
                "Exemple: `/bet 100`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
            
        # Validation de la mise
        try:
            bet = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Le montant doit être un nombre valide.")
            return
        
        if bet < 10:
            await update.message.reply_text("❌ La mise minimum est de 10 💵")
            return
            
        if bet > 50000:
            await update.message.reply_text("❌ La mise maximum est de 50 000 💵")
            return
            
        # Vérification du solde
        balance = db.get_balance(user.id)
        
        if balance < bet:
            await update.message.reply_text(
                f"❌ Fonds insuffisants\n"
                f"└ Solde: {balance} 💵"
            )
            return
            
        # Création des boutons
        keyboard = [
            [InlineKeyboardButton("Rejoindre la partie 🎲", callback_data=f"join_dice_{bet}")],
            [InlineKeyboardButton("Annuler ❌", callback_data="cancel_dice")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Échapper les caractères spéciaux dans le nom d'utilisateur
        display_name = user.username if user.username else user.first_name
        display_name = display_name.replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')
        
        # Envoi du message et stockage des références
        message = await update.message.reply_text(
            f"🎲 *Nouvelle partie de dés*\n"
            f"├ Créée par: {display_name}\n"
            f"├ Mise: {bet} 💵\n"
            f"└ En attente d'un adversaire... (expire dans 5 min)",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
            message_thread_id=thread_id
        )

        # Création de la partie avec ID unique
        game_id = f"{chat_id}_{message.message_id}"
        dice_games[game_id] = {
            'host': user,
            'bet': bet,
            'message_id': message.message_id,
            'context': context,
            'message': message,
            'created_at': datetime.now()
        }
        user_games[user.id] = game_id
                
        # Création de la tâche d'expiration
        asyncio.create_task(cancel_game_after_delay(game_id, message.message_id))
        
    except Exception as e:
        print(f"Error in bet: {e}")
        await update.message.reply_text("❌ Une erreur s'est produite.")
        # Ne pas essayer de supprimer game_id s'il n'existe pas encore
        if user.id in user_games:
            game_id = user_games[user.id]
            if game_id in dice_games:
                del dice_games[game_id]
            del user_games[user.id]

async def handle_dice_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    thread_id = query.message.message_thread_id if query.message else None
    
    try:
        bet = int(query.data.split('_')[2])
        game_id = f"{chat_id}_{query.message.message_id}"
        
        if game_id not in dice_games:
            await query.answer("❌ Cette partie n'existe plus.", show_alert=True)
            return
            
        game = dice_games[game_id]
        
        if user.id == game['host'].id:
            await query.answer("❌ Vous ne pouvez pas rejoindre votre propre partie!", show_alert=True)
            return
            
        balance = db.get_balance(user.id)
        
        if balance < bet:
            await query.answer("❌ Fonds insuffisants!", show_alert=True)
            return
            
        # Supprimer le jeu et la référence de l'utilisateur
        host_id = game['host'].id
        del dice_games[game_id]
        del user_games[host_id]
        
        # Lancer la partie
        host_name = escape_markdown(game['host'].username if game['host'].username else game['host'].first_name)
        player_name = escape_markdown(user.username if user.username else user.first_name)
        
        # Envoyer le nouveau message avant de supprimer l'ancien
        game_message = await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ *Partie commencée*\n"
                 f"🎲 {host_name} VS {player_name}",
            parse_mode=ParseMode.MARKDOWN,
            message_thread_id=thread_id
        )
        
        # Supprimer le message de la partie en attente
        await query.message.delete()
        
        # Lancer les dés
        host_dice = await context.bot.send_dice(
            chat_id=chat_id,
            emoji='🎲',
            message_thread_id=thread_id
        )
        host_value = host_dice.dice.value

        await asyncio.sleep(4)

        opponent_dice = await context.bot.send_dice(
            chat_id=chat_id,
            emoji='🎲',
            message_thread_id=thread_id
        )
        opponent_value = opponent_dice.dice.value
        
        await asyncio.sleep(4)
     
        try:
            if host_value > opponent_value:
                winner = game['host']
                winner_value = host_value
                loser = user
                loser_value = opponent_value
            elif opponent_value > host_value:
                winner = user
                winner_value = opponent_value
                loser = game['host']
                loser_value = host_value
            else:
                # En cas d'égalité
                host_balance = db.get_balance(game['host'].id)
                opponent_balance = db.get_balance(user.id)
                
                host_name = escape_markdown(game['host'].username if game['host'].username else game['host'].first_name)
                player_name = escape_markdown(user.username if user.username else user.first_name)
                await game_message.reply_text(
                    f"🎲 *Égalité!*\n"
                    f"├ {host_name}: {host_value}\n"
                    f"├ {player_name}: {opponent_value}\n"
                    f"├ Les mises sont remboursées\n"
                    f"│\n"
                    f"💰 *Soldes actuels*\n"
                    f"├ {game['host'].username if game['host'].username else game['host'].first_name}: {host_balance} 💵\n"
                    f"└ {user.username if user.username else user.first_name}: {opponent_balance} 💵",
                    parse_mode=ParseMode.MARKDOWN,
                    message_thread_id=thread_id
                )
                return
                
            # Mettre à jour les soldes
            db.add_balance(winner.id, bet)
            db.add_balance(loser.id, -bet)
            
            # Récupérer les nouveaux soldes
            winner_balance = db.get_balance(winner.id)
            loser_balance = db.get_balance(loser.id)
            
            winner_name = escape_markdown(winner.username if winner.username else winner.first_name)
            loser_name = escape_markdown(loser.username if loser.username else loser.first_name)
            await game_message.reply_text(
                f"🎲 *Résultat de la partie*\n"
                f"├ {winner_name}: {winner_value}\n"
                f"├ {loser_name}: {loser_value}\n"
                f"├ Gagnant: {winner_name}\n"
                f"├ Gains: +{bet} 💵\n"
                f"│\n"
                f"💰 *Nouveaux soldes*\n"
                f"├ {winner.username if winner.username else winner.first_name}: {winner_balance} 💵\n"
                f"└ {loser.username if loser.username else loser.first_name}: {loser_balance} 💵",
                parse_mode=ParseMode.MARKDOWN
            )

            if user.id in user_games:
                del user_games[user.id]
            if host_id in user_games:
                del user_games[host_id]
            if game_id in dice_games:
                del dice_games[game_id]
            
        except Exception as e:
            print(f"Error in game resolution: {e}")
            await game_message.reply_text("❌ Une erreur s'est produite lors de la résolution de la partie.")
            
    except Exception as e:
        print(f"Error in handle_dice_join: {e}")
        await query.answer("❌ Une erreur s'est produite.", show_alert=True)

async def handle_dice_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    chat_id = query.message.chat_id
    
    try:
        game_id = f"{chat_id}_{query.message.message_id}"
        if game_id not in dice_games:
            await query.answer("❌ Cette partie n'existe plus.", show_alert=True)
            return
            
        game = dice_games[game_id]
        
        if user.id != game['host'].id:
            await query.answer("❌ Seul le créateur peut annuler la partie!", show_alert=True)
            return
            
        try:
            display_name = escape_markdown(user.username if user.username else user.first_name)
            await query.message.edit_text(
                f"🎲 *Partie annulée*\n"
                f"└ Annulée par: @{display_name}",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            print(f"Error editing message: {e}")
        finally:
            del dice_games[game_id]
            del user_games[user.id]
        
    except Exception as e:
        print(f"Error in handle_dice_cancel: {e}")
        await query.answer("❌ Une erreur s'est produite.", show_alert=True)

async def cancel_game_after_delay(game_id: str, message_id: int):
    await asyncio.sleep(300)  # 5 minutes
    
    try:
        if game_id in dice_games:
            game = dice_games[game_id]
            if game['message_id'] == message_id:
                try:
                    await game['message'].edit_text(
                        f"🎲 *Partie expirée*\n└ Temps d'attente dépassé (5 min)",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    print(f"Erreur lors de la modification du message expiré: {e}")
                finally:
                    # Nettoyer les références
                    host_id = game['host'].id
                    if host_id in user_games:
                        del user_games[host_id]
                    del dice_games[game_id]
                    
    except Exception as e:
        print(f"Error in cancel_game_after_delay: {e}")

async def dice_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message else None
    
    stats_text = (
        "*🎲 Règles du jeu de dés 1v1*\n"
        "├ Chaque joueur lance un dé\n"
        "├ Le plus grand nombre gagne\n"
        "├ Le gagnant remporte la mise de l'adversaire\n"
        "└ En cas d'égalité, les mises sont remboursées\n\n"
        "💡 *Autres informations*\n"
        "├ Mise minimum : 10 💵\n"
        "├ Mise maximum : 50 000 💵\n"
        "└ Délai d'attente : 5 minutes"
    )
    
    await update.message.reply_text(
        stats_text, 
        parse_mode=ParseMode.MARKDOWN,
        message_thread_id=thread_id
    )

def main():
    
    # Commandes du jeu de dés
    application.add_handler(CommandHandler("bet", bet))
    application.add_handler(CommandHandler("dicestats", dice_stats))
    application.add_handler(CommandHandler("dropcredits", dropcredits))
    # Gestionnaires de boutons
    application.add_handler(CallbackQueryHandler(handle_dice_join, pattern="^join_dice_"))
    application.add_handler(CallbackQueryHandler(handle_dice_cancel, pattern="^cancel_dice"))
    application.add_handler(CallbackQueryHandler(handle_credit_claim, pattern="^claim_credits_"))
    # Démarrer le bot
    application.run_polling()

if __name__ == '__main__':
    main()
