import logging
import random
import asyncio
import sqlite3
import pytz
from command_delete_handler import CommandDeleteHandler
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, Defaults, filters

# Variables globales
active_games = {}
waiting_games = set()
game_messages = {}
CLASSEMENT_MESSAGE_ID = None  # Ajoutez cette ligne
CLASSEMENT_CHAT_ID = None     # Ajoutez cette ligne
last_game_message = {}  # {chat_id: message_id}
last_end_game_message = {}  # {chat_id: message_id}

# Configuration du logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

 
GAME_THREAD_ID = 6
FORBIDDEN_THREADS = [1, 2]
INITIAL_BALANCE = 1500
MAX_PLAYERS = 20
game_messages = {}  # Pour stocker l'ID du message de la partie en cours

class Card:
    def __init__(self, rank, suit):
        self.rank = rank
        self.suit = suit
        
    def __str__(self):
        suits = {'♠': '♠️', '♥': '♥️', '♦': '♦️', '♣': '♣️'}
        return f"{self.rank}{suits[self.suit]}"

class Deck:
    def __init__(self):
        ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
        suits = ['♠', '♥', '♦', '♣']
        self.cards = [Card(rank, suit) for rank in ranks for suit in suits]
        random.shuffle(self.cards)
    
    def deal(self):
        if not self.cards:
            # Recréer un deck si vide
            self.__init__()
        return self.cards.pop()

class MultiPlayerGame:
    def __init__(self, host_id, host_name=None):  # Ajout du paramètre host_name avec une valeur par défaut
        self.host_id = host_id
        self.host_name = host_name  # Stockage du nom de l'hôte
        self.players = {}
        self.dealer_hand = []
        self.deck = Deck()
        self.game_status = 'waiting'
        self.bet_amount = None  # Pour stocker la mise initiale
        self.created_at = datetime.utcnow()  # Pour tracker le temps de création

    def add_player(self, player_id, bet):
        if len(self.players) < MAX_PLAYERS and player_id not in self.players:
            self.players[player_id] = {
                'hand': [],
                'bet': bet,
                'status': 'playing'
            }
            return True
        return False

    def check_timeout(self):
            """Vérifie si le joueur actuel a dépassé le temps imparti"""
            if hasattr(self, 'last_action_time'):
                current_time = datetime.utcnow()
                time_difference = (current_time - self.last_action_time).total_seconds()
                return time_difference > 30
            return False

    def can_split(self, player_id):
        player_data = self.players[player_id]
        # Vérifie si le joueur a exactement deux cartes de même rang
        return len(player_data['hand']) == 2 and player_data['hand'][0].rank == player_data['hand'][1].rank

    def split_hand(self, player_id):
        player_data = self.players[player_id]
        if self.can_split(player_id):
            new_hand = [player_data['hand'].pop()]
            player_data['hand'] = [player_data['hand'][0], self.deck.deal()]
            player_data['second_hand'] = [new_hand[0], self.deck.deal()]
            player_data['status'] = 'playing'
            return True
        return False

    def get_host_name(self) -> str:
        """Retourne le nom de l'hôte de la partie"""
        return self.host_name if self.host_name else "Inconnu"
        
    def get_bet(self) -> int:
        """Retourne la mise de la partie"""
        if self.players and self.host_id in self.players:
            return self.players[self.host_id]['bet']
        return 0

    def is_expired(self) -> bool:
        """Vérifie si la partie a expiré (plus de 5 minutes)"""
        if self.game_status != 'waiting':
            return False
        time_diff = datetime.utcnow() - self.created_at
        return time_diff.total_seconds() >= 300  # 5 minutes

    def add_player(self, player_id, bet):
        if len(self.players) < MAX_PLAYERS and player_id not in self.players:
            self.players[player_id] = {
                'hand': [],
                'bet': bet,
                'status': 'playing'
            }
            if player_id == self.host_id:  # Si c'est l'hôte, on stocke la mise initiale
                self.bet_amount = bet
            return True
        return False

    def calculate_hand(self, hand):
        """Calcule la valeur d'une main"""
        if not hand:
            return 0
            
        value = 0
        aces = 0
        
        for card in hand:
            if card.rank in ['J', 'Q', 'K']:
                value += 10
            elif card.rank == 'A':
                aces += 1
            else:
                value += int(card.rank)
        
        # Ajouter les as avec la meilleure valeur possible
        for _ in range(aces):
            if value + 11 <= 21:
                value += 11
            else:
                value += 1
                
        return value

    def get_current_player_id(self):
        for player_id, player_data in self.players.items():
            if player_data['status'] == 'playing':
                return player_id
        return None

    def next_player(self):
        """Passe au joueur suivant"""
        current_player_id = self.get_current_player_id()
        found_current = False
        has_next_player = False
        
        # Créer une liste ordonnée des joueurs
        player_ids = list(self.players.keys())
        if current_player_id in player_ids:
            current_index = player_ids.index(current_player_id)
            # Chercher le prochain joueur à partir du joueur actuel
            for i in range(current_index + 1, len(player_ids)):
                next_player_id = player_ids[i]
                if self.players[next_player_id]['status'] == 'playing':
                    has_next_player = True
                    break
        
        # Si aucun prochain joueur n'est trouvé
        if not has_next_player:
            # Vérifier si tous les joueurs ont terminé
            all_finished = True
            for player_data in self.players.values():
                if player_data['status'] == 'playing':
                    all_finished = False
                    break
            
            if all_finished:
                self.game_status = 'finished'
                self.resolve_dealer()
                self.determine_winners()
                return True  # Indique que la partie est terminée
        return False  # La partie continue

    def start_game(self):
        """Démarre la partie"""
        if len(self.players) < 1:
            return False
    
        self.game_status = 'playing'
        self.deal_initial_cards()
        self.last_action_time = datetime.utcnow()
        # Compter combien de joueurs sont encore actifs
        active_players = 0
        for player_id, player_data in self.players.items():
            if self.calculate_hand(player_data['hand']) == 21:
                player_data['status'] = 'blackjack'
            else:
                active_players += 1
                player_data['status'] = 'playing'
    
        # Ne terminer la partie que si TOUS les joueurs ont un blackjack
        if active_players == 0:
            self.game_status = 'finished'
            self.resolve_dealer()
            self.determine_winners()
    
        return True

    def deal_initial_cards(self):
        """Distribution initiale des cartes"""
        # Distribuer aux joueurs
        for player_id in self.players:
            self.players[player_id]['hand'] = [self.deck.deal(), self.deck.deal()]
            if self.calculate_hand(self.players[player_id]['hand']) == 21:
                self.players[player_id]['status'] = 'blackjack'
        
        # Distribuer au croupier
        self.dealer_hand = [self.deck.deal(), self.deck.deal()]
        self.last_action_time = datetime.utcnow() 
    def resolve_dealer(self):
        """Tour du croupier"""
        while self.calculate_hand(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.deal())
            
    def determine_winners(self):
        """Tour du croupier"""
        dealer_total = self.calculate_hand(self.dealer_hand)
        dealer_bust = dealer_total > 21

        for player_id, player_data in self.players.items():
            # Traiter la main principale
            if player_data['status'] == 'bust':
                player_data['first_status'] = 'bust'  # Stocker le statut explicitement
                db.update_game_result(player_id, player_data['bet'], 'lose')
            elif player_data['status'] == 'blackjack':
                player_data['first_status'] = 'blackjack'  # Stocker le statut explicitement
                db.update_game_result(player_id, player_data['bet'], 'blackjack')
            elif player_data['status'] == 'stand':
                player_total = self.calculate_hand(player_data['hand'])
            
                if dealer_bust:
                    player_data['first_status'] = 'win'
                    db.update_game_result(player_id, player_data['bet'], 'win')
                elif player_total > dealer_total:
                    player_data['first_status'] = 'win'
                    db.update_game_result(player_id, player_data['bet'], 'win')
                elif player_total < dealer_total:
                    player_data['first_status'] = 'lose'
                    db.update_game_result(player_id, player_data['bet'], 'lose')
                else:
                    player_data['first_status'] = 'push'
                    db.update_game_result(player_id, player_data['bet'], 'push')
        
            # Traiter la seconde main si elle existe
            if 'second_hand' in player_data:
                second_total = self.calculate_hand(player_data['second_hand'])
                if second_total > 21:
                    player_data['second_status'] = 'bust'
                    db.update_game_result(player_id, player_data['bet'], 'lose')
                elif dealer_bust:
                    player_data['second_status'] = 'win'
                    db.update_game_result(player_id, player_data['bet'], 'win')
                elif second_total > dealer_total:
                    player_data['second_status'] = 'win'
                    db.update_game_result(player_id, player_data['bet'], 'win')
                elif second_total < dealer_total:
                    player_data['second_status'] = 'lose'
                    db.update_game_result(player_id, player_data['bet'], 'lose')
                else:
                    player_data['second_status'] = 'push'
                    db.update_game_result(player_id, player_data['bet'], 'push')

class DatabaseManager:
    def __init__(self):
        self.conn = sqlite3.connect('blackjack.db', check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.setup_database()
    
    def setup_database(self):
        self.cursor.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 1000,
                games_played INTEGER DEFAULT 0,
                games_won INTEGER DEFAULT 0,
                total_bets INTEGER DEFAULT 0,
                biggest_win INTEGER DEFAULT 0,
                last_daily DATETIME
            );
            
            CREATE TABLE IF NOT EXISTS game_history (
                game_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                bet_amount INTEGER,
                result TEXT,
                timestamp DATETIME,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            );
        ''')
        self.conn.commit()

    def register_user(self, user_id: int, username: str) -> bool:
        """Inscrit un nouvel utilisateur dans la base de données"""
        try:
            self.cursor.execute('''
                INSERT INTO users (
                    user_id, 
                    username, 
                    balance, 
                    games_played,
                    games_won,
                    total_bets,
                    biggest_win,
                    last_daily
                ) VALUES (?, ?, 1000, 0, 0, 0, 0, ?)
            ''', (user_id, username, '2000-01-01 00:00:00'))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Si l'utilisateur existe déjà
            return False
        except Exception as e:
            print(f"Erreur dans register_user: {e}")
            self.conn.rollback()
            return False

    def get_username(self, user_id: int) -> str:
        """Récupère le nom d'utilisateur"""
        try:
            self.cursor.execute('SELECT username FROM users WHERE user_id = ?', (user_id,))
            result = self.cursor.fetchone()
            return result[0] if result else None
        except Exception as e:
            print(f"Erreur dans get_username: {e}")
            return None

    def update_username(self, user_id: int, new_username: str) -> bool:
        """Met à jour le nom d'utilisateur"""
        try:
            self.cursor.execute('UPDATE users SET username = ? WHERE user_id = ?', 
                            (new_username, user_id))
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Erreur dans update_username: {e}")
            self.conn.rollback()
            return False

    def get_player_rank(self, balance: int) -> tuple[str, str, float, Optional[str]]:
        """
        Retourne le rang du joueur basé sur son solde
        Returns: (emoji, titre, progression, prochain_rang)
        """
        ranks = [
            (0, "🤡 Clochard du Casino"),
            (500, "🎲 Joueur Amateur"),
            (1000, "🎰 Joueur Lambda"),
            (2500, "💰 Petit Parieur"),
            (5000, "💎 Parieur Régulier"),
            (10000, "🎩 High Roller"),
            (25000, "👑 Roi du Casino"),
            (50000, "🌟 VIP Diamond"),
            (100000, "🔥 Parieur Fou"),
            (250000, "🌈 Légende du Casino"),
            (500000, "⚡ Master des Tables"),
            (1000000, "🌌 Empereur du Gambling")
        ]
        
        current_rank = ranks[0]
        for threshold, rank in ranks:
            if balance >= threshold:
                current_rank = (threshold, rank)
            else:
                break
                
        current_index = ranks.index(current_rank)
        next_rank = ranks[current_index + 1] if current_index < len(ranks) - 1 else None
        
        emoji, title = current_rank[1].split(" ", 1)
        
        if next_rank:
            current_threshold = current_rank[0]
            next_threshold = next_rank[0]
            progress = ((balance - current_threshold) / (next_threshold - current_threshold)) * 100
            progress = min(100, max(0, progress))
        else:
            progress = 100

        return emoji, title, progress, next_rank[1] if next_rank else None

    def get_balance(self, user_id: int) -> int:
        """Récupère le solde d'un utilisateur"""
        try:
            cursor = self.conn.cursor()
            cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            cursor.close()
            return result[0] if result else 0
        except Exception as e:
            print(f"Erreur dans get_balance: {e}")
            return 0

    def set_balance(self, user_id: int, amount: int) -> None:
        """Met à jour le solde d'un utilisateur"""
        try:
            cursor = self.conn.cursor()
            cursor.execute('UPDATE users SET balance = ? WHERE user_id = ?', (amount, user_id))
            self.conn.commit()
            cursor.close()
        except Exception as e:
            print(f"Erreur dans set_balance: {e}")
            self.conn.rollback()

    def user_exists(self, user_id: int) -> bool:
        """Vérifie si un utilisateur existe dans la base de données"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT 1 FROM users WHERE user_id = ?', (user_id,))
        exists = cursor.fetchone() is not None
        cursor.close()
        return exists

    def get_games_played(self, user_id: int) -> int:
        """Récupère le nombre total de parties jouées"""
        try:
            cursor = self.conn.cursor()
            cursor.execute('SELECT games_played FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            cursor.close()
            return result[0] if result else 0
        except Exception as e:
            print(f"Erreur dans get_games_played: {e}")
            return 0

    def get_wins(self, user_id: int) -> int:
        """Récupère le nombre total de victoires"""
        try:
            cursor = self.conn.cursor()
            cursor.execute('SELECT games_won FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            cursor.close()
            return result[0] if result else 0
        except Exception as e:
            print(f"Erreur dans get_wins: {e}")
            return 0

    def get_stats(self, user_id: int) -> dict:
        """Récupère toutes les statistiques d'un joueur"""
        try:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT balance, games_played, games_won, total_bets, biggest_win
                FROM users WHERE user_id = ?
            ''', (user_id,))
            result = cursor.fetchone()
            cursor.close()
            
            if result:
                return {
                    'balance': result[0],
                    'games_played': result[1],
                    'games_won': result[2],
                    'total_bets': result[3],
                    'biggest_win': result[4]
                }
            return {
                'balance': 0,
                'games_played': 0,
                'games_won': 0,
                'total_bets': 0,
                'biggest_win': 0
            }
        except Exception as e:
            print(f"Erreur dans get_stats: {e}")
            return {}

    def update_game_result(self, user_id: int, bet_amount: int, result: str):
        """Met à jour les statistiques après une partie"""
        multiplier = {
            'win': 2,
            'blackjack': 2.5,
            'lose': 0,
            'push': 1
        }.get(result, 0)
        
        winnings = int(bet_amount * multiplier)
        
        self.cursor.execute('''
            UPDATE users 
            SET balance = balance + ?,
                games_played = games_played + 1,
                games_won = games_won + CASE WHEN ? IN ('win', 'blackjack') THEN 1 ELSE 0 END,
                total_bets = total_bets + ?,
                biggest_win = CASE 
                    WHEN ? > biggest_win AND ? IN ('win', 'blackjack')
                    THEN ? ELSE biggest_win 
                END
            WHERE user_id = ?
        ''', (winnings - bet_amount, result, bet_amount, winnings, result, winnings, user_id))
        
        self.cursor.execute('''
            INSERT INTO game_history (user_id, bet_amount, result, timestamp)
            VALUES (?, ?, ?, ?)
        ''', (user_id, bet_amount, result, datetime.utcnow()))
        
        self.conn.commit()

    def can_claim_daily(self, user_id: int) -> tuple[bool, Optional[timedelta]]:
        """Vérifie si l'utilisateur peut réclamer sa récompense journalière"""
        try:
            cursor = self.conn.cursor()
            cursor.execute('SELECT last_daily FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            cursor.close()
            
            if not result or not result[0]:
                return True, None
                
            last_daily = datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
            now = datetime.utcnow()
            time_diff = now - last_daily
            
            if time_diff.total_seconds() >= 2 * 3600:  # 24 heures
                return True, None
            else:
                time_remaining = timedelta(days=1) - time_diff
                return False, time_remaining
                
        except Exception as e:
            print(f"Erreur dans can_claim_daily: {e}")
            return False, None

    def claim_daily(self, user_id: int, amount: int) -> bool:
        """Donne la récompense journalière à l'utilisateur"""
        try:
            cursor = self.conn.cursor()
            cursor.execute('''
                UPDATE users 
                SET balance = balance + ?,
                    last_daily = ?
                WHERE user_id = ?
            ''', (amount, datetime.utcnow(), user_id))
            self.conn.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"Erreur dans claim_daily: {e}")
            self.conn.rollback()
            return False

    def close(self):
        """Ferme la connexion à la base de données"""
        self.conn.close()


    def close(self):
        """Ferme la connexion à la base de données"""
        self.conn.close()

db = DatabaseManager()
active_games: Dict[int, MultiPlayerGame] = {}  # {host_id: game}
waiting_games: Set[int] = set()  # host_ids of games waiting for players

def is_admin(user_id: int):
    """Vérifie si l'utilisateur est administrateur"""
    return user_id in ADMIN_USERS

def cleanup_player_games(player_id):
    """Nettoie toutes les références à un joueur dans les parties actives"""
    for game_id in list(active_games.keys()):
        if player_id in active_games[game_id].players:
            del active_games[game_id]
    if player_id in waiting_games:
        waiting_games.remove(player_id)



def get_player_rank(balance: int) -> tuple[str, str]:
    """
    Retourne le rang du joueur basé sur son solde
    Returns: (emoji, titre)
    """
    ranks = [
        (0, "🤡 Clochard du Casino"),
        (500, "🎲 Joueur Amateur"),
        (1000, "🎰 Joueur Lambda"),
        (2500, "💰 Petit Parieur"),
        (5000, "💎 Parieur Régulier"),
        (10000, "🎩 High Roller"),
        (25000, "👑 Roi du Casino"),
        (50000, "🌟 VIP Diamond"),
        (100000, "🔥 Parieur Fou"),
        (250000, "🌈 Légende du Casino"),
        (500000, "⚡ Master des Tables"),
        (1000000, "🌌 Empereur du Gambling")
    ]
    
    current_rank = ranks[0]  # Rang par défaut
    for threshold, rank in ranks:
        if balance >= threshold:
            current_rank = (threshold, rank)
        else:
            break
            
    # Trouve le prochain rang
    current_index = ranks.index(current_rank)
    next_rank = ranks[current_index + 1] if current_index < len(ranks) - 1 else None
    
    # Sépare l'emoji du titre
    emoji, title = current_rank[1].split(" ", 1)
    
    # Calcule la progression vers le prochain rang
    if next_rank:
        current_threshold = current_rank[0]
        next_threshold = next_rank[0]
        progress = ((balance - current_threshold) / (next_threshold - current_threshold)) * 100
        progress = min(100, max(0, progress))  # Garde entre 0 et 100
    else:
        progress = 100

    return emoji, title, progress, next_rank[1] if next_rank else None
    
def get_status_emoji(status: str) -> str:
    emojis = {
        'waiting': '⏳',
        'playing': '🎮',
        'bust': '💥',
        'stand': '⏹',
        'blackjack': '🌟',
        'win': '🎉',
        'lose': '💀',
        'push': '🤝'
    }
    return f"{emojis.get(status, '❓')} {status.upper()}"

async def handle_forbidden_thread(message) -> bool:
    """Supprime silencieusement le message si c'est dans un thread interdit"""
    if message.message_thread_id in FORBIDDEN_THREADS:
        try:
            await message.delete()
        except Exception:
            pass  # Ignore silencieusement les erreurs de suppression
        return True
    return False

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Démarre le bot et inscrit le joueur s'il ne l'est pas déjà"""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_forbidden_thread(update.message): return

    # Vérifier si l'utilisateur existe déjà dans la base de données
    if not db.user_exists(user.id):
        # Inscrire l'utilisateur avec les valeurs par défaut
        if db.register_user(user.id, user.first_name):
            welcome_message = (
                f"👋 Bienvenue {user.first_name} !\n\n"
                f"💰 Je vous offre 1000 coins pour commencer !\n\n"
                f"Commandes disponibles :\n"
                f"/bank - Voir votre solde\n"
                f"/daily - Réclamer votre bonus quotidien\n"
                f"/stats - Voir vos statistiques"
            )
        else:
            welcome_message = "❌ Une erreur s'est produite lors de votre inscription. Réessayez plus tard."
    else:
        # Obtenir les statistiques du joueur existant
        stats = db.get_stats(user.id)
        emoji, title, progress, next_rank = db.get_player_rank(stats['balance'])
        
        welcome_message = (
            f"👋 Re-bonjour {user.first_name} !\n\n"
            f"💰 Votre solde : {stats['balance']} coins\n"
            f"{emoji} Rang : {title}\n"
        )
        
        if next_rank:
            welcome_message += f"📈 Progression : {progress:.1f}% vers {next_rank}\n"
        
        welcome_message += (
            f"\nCommandes disponibles :\n"
            f"/bank - Voir votre solde\n"
            f"/daily - Réclamer votre bonus quotidien\n"
            f"/stats - Voir vos statistiques"
        )

    await update.message.reply_text(welcome_message)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    stats = db.get_stats(user.id)
    
    emoji, rank_title, progress, next_rank = db.get_player_rank(stats['balance'])
    
    # Barre de progression
    progress_bar_length = 10
    filled_length = int(progress_bar_length * progress / 100)
    progress_bar = "█" * filled_length + "░" * (progress_bar_length - filled_length)
    
    win_rate = (stats['games_won'] / stats['games_played'] * 100) if stats['games_played'] > 0 else 0
    
    stats_text = (
        f"*STATISTIQUES DE {user.first_name}*\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"💵 *Solde:* {stats['balance']:,} $\n"
        f"🎖️ *Rang:* {emoji} {rank_title}\n"
    )
    
    if next_rank:
        stats_text += (
            f"\n*Progression vers {next_rank}*\n"
            f"[{progress_bar}] {progress:.1f}%\n"
        )
    else:
        stats_text += "\n🏆 *Rang Maximum Atteint !*\n"
    
    stats_text += (
        f"\n📊 *Statistiques de Jeu*\n"
        f"├ Parties jouées: {stats['games_played']}\n"
        f"├ Victoires: {stats['games_won']}\n"
        f"├ Taux de victoire: {win_rate:.1f}%\n"
        f"├ Total parié: {stats['total_bets']:,} $\n"
        f"└ Plus gros gain: {stats['biggest_win']:,} $\n"
    )
    
    await update.message.reply_text(
        stats_text,
        parse_mode=ParseMode.MARKDOWN
    )

async def set_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande administrative pour définir les crédits d'un joueur
    Usage: /setcredits [user_id] montant"""
    
    user = update.effective_user
    
    if not is_admin(user.id):
        await update.message.reply_text("❌ Cette commande est réservée aux administrateurs.")
        return
        
    try:
        # Vérifier les arguments
        if len(context.args) != 2:
            await update.message.reply_text(
                "❌ Usage incorrect.\n"
                "Usage: `/setcredits [user_id] montant`\n"
                "Exemple: `/setcredits 123456789 1000`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
            
        # Récupérer l'ID utilisateur
        target_id = int(context.args[0])
        amount = int(context.args[1])
        
        if amount < 0:
            await update.message.reply_text("❌ Le montant doit être positif.")
            return
            
        # Vérifier si l'utilisateur existe
        if not db.user_exists(target_id):
            await update.message.reply_text("❌ Utilisateur non trouvé.")
            return
            
        db.set_balance(target_id, amount)
        
        await update.message.reply_text(
            f"✅ *Crédits modifiés*\n"
            f"└ ID {target_id}: {amount} 💵",
            parse_mode=ParseMode.MARKDOWN
        )
        
    except ValueError:
        await update.message.reply_text("❌ Le montant doit être un nombre valide.")
    except Exception as e:
        print(f"Error in set_credits: {e}")
        await update.message.reply_text("❌ Une erreur s'est produite.")

async def add_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande administrative pour ajouter des crédits à un joueur
    Usage: /addcredits [user_id] montant"""
    
    user = update.effective_user
    
    if not is_admin(user.id):
        await update.message.reply_text("❌ Cette commande est réservée aux administrateurs.")
        return
        
    try:
        # Vérifier les arguments
        if len(context.args) != 2:
            await update.message.reply_text(
                "❌ Usage incorrect.\n"
                "Usage: `/addcredits [user_id] montant`\n"
                "Exemple: `/addcredits 123456789 1000`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
            
        # Récupérer l'ID utilisateur
        target_id = int(context.args[0])
        amount = int(context.args[1])
        
        # Vérifier si l'utilisateur existe
        if not db.user_exists(target_id):
            await update.message.reply_text("❌ Utilisateur non trouvé.")
            return
            
        current_balance = db.get_balance(target_id)
        new_balance = current_balance + amount
        
        if new_balance < 0:
            await update.message.reply_text("❌ Le solde ne peut pas être négatif.")
            return
            
        db.set_balance(target_id, new_balance)
        
        # Emoji en fonction si on ajoute ou retire des crédits
        operation_emoji = "➕" if amount >= 0 else "➖"
        amount_abs = abs(amount)
        
        await update.message.reply_text(
            f"✅ *Crédits modifiés*\n"
            f"├ ID {target_id}\n"
            f"├ {operation_emoji} {amount_abs} 💵\n"
            f"└ Nouveau solde: {new_balance} 💵",
            parse_mode=ParseMode.MARKDOWN
        )
        
    except ValueError:
        await update.message.reply_text("❌ Le montant doit être un nombre valide.")
    except Exception as e:
        print(f"Error in add_credits: {e}")
        await update.message.reply_text("❌ Une erreur s'est produite.")

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    current_time = datetime.utcnow()
    
    db.cursor.execute('SELECT last_daily FROM users WHERE user_id = ?', (user.id,))
    result = db.cursor.fetchone()
    
    if await handle_forbidden_thread(update.message): return

    if result and result[0]:
        last_daily = datetime.fromisoformat(result[0])
        if current_time - last_daily < timedelta(days=1):
            next_daily = last_daily + timedelta(days=1)
            time_remaining = next_daily - current_time
            hours, remainder = divmod(time_remaining.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            
            await update.message.reply_text(
                f"⏳ *Bonus non disponible*\n\n"
                f"Revenez dans:\n"
                f"▫️ {hours}h {minutes}m {seconds}s",
                parse_mode=ParseMode.MARKDOWN
            )
            return

    bonus = 1000
    db.cursor.execute('''
        UPDATE users 
        SET balance = balance + ?, 
            last_daily = ?
        WHERE user_id = ?
    ''', (bonus, current_time, user.id))
    db.conn.commit()
    
    await update.message.reply_text(
        f"🎁 *BONUS QUOTIDIEN !*\n\n"
        f"💰 +{bonus} coins ajoutés à votre compte\n"
        f"💳 Nouveau solde: {db.get_balance(user.id)} coins",
        parse_mode=ParseMode.MARKDOWN
    )

async def create_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message or update.edited_message
    chat_id = message.chat_id
    
    # Utilisez le THREAD ID spécifique pour les messages de jeu
    message_thread_id = GAME_THREAD_ID
    if await handle_forbidden_thread(update.message): return
    # Vérifier s'il y a déjà une partie en cours dans ce chat
    for g in active_games.values():
        if hasattr(g, 'initial_chat_id') and g.initial_chat_id == chat_id and g.game_status == 'playing':
            error_msg = await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                text="❌ Une partie est déjà en cours dans ce chat!"
            )
            await message.delete()
            await asyncio.sleep(3)
            await error_msg.delete()
            return

    # Supprimer UNIQUEMENT le message "partie terminée" s'il existe
    if chat_id in last_end_game_message:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=last_end_game_message[chat_id])
            del last_end_game_message[chat_id]
        except Exception:
            pass
    try:
        bet_amount = int(context.args[0])
    except (IndexError, ValueError):
        error_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Veuillez spécifier une mise valide.\n"
                 "Usage: `/bj [mise]`\n"
                 "Exemple: `/bj 100`",
            parse_mode=ParseMode.MARKDOWN
        )
        await message.delete()
        await asyncio.sleep(3)
        await error_msg.delete()
        return
    
    if bet_amount < 10 or bet_amount > 1000000:
        error_msg = await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            text="❌ La mise doit être entre 10 et 1000000 coins."
        )
        await message.delete()
        await asyncio.sleep(3)
        await error_msg.delete()
        return
    
    # Vérifier le solde
    balance = db.get_balance(user.id)
    if balance < bet_amount:
        error_msg = await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            text=f"❌ Solde insuffisant!\n"
                 f"Votre solde: {balance} coins"
        )
        await message.delete()
        await asyncio.sleep(3)
        await error_msg.delete()
        return

    # Vérifier s'il y a une partie en attente
    existing_game = None
    for g in active_games.values():
        if g.game_status == 'waiting':
            existing_game = g
            break

    if existing_game:
        # Rejoindre la partie existante
        if user.id in existing_game.players:
            error_msg = await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                text="❌ Vous êtes déjà dans cette partie!"
            )
            await message.delete()
            await asyncio.sleep(3)
            await error_msg.delete()
            return

        if existing_game.add_player(user.id, bet_amount):
            await message.delete()
            
            # Préparer le texte des joueurs
            players_text = "*👥 JOUEURS:*\n"
            total_bets = 0

            for player_id, player_data in existing_game.players.items():
                player = await context.bot.get_chat(player_id)
                emoji, rank_title, _, _ = db.get_player_rank(db.get_balance(player_id))
                bet = player_data['bet']
                total_bets += bet
                
                players_text += (
                    f"└ {emoji} {player.first_name} ➜ {bet} 💵\n"
                    f"   ├ Rang: {rank_title}\n"
                    f"   └ Gains possibles:\n"
                    f"      ├ Blackjack: +{int(bet * 2.5)} 💵\n"
                    f"      └ Victoire: +{bet * 2} 💵\n"
                )

            # Créer le keyboard markup pour le créateur
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎮 LANCER LA PARTIE", callback_data="start_game")
            ]])

            # Supprimer l'ancien message s'il existe
            if chat_id in last_game_message:
                try:
                    await context.bot.delete_message(chat_id, last_game_message[chat_id])
                except:
                    pass

            # Envoyer le nouveau message
            new_message = await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                text=f"*🎰 PARTIE EN ATTENTE*\n"
                     f"━━━━━━━━━━\n\n"
                     f"{players_text}\n"
                     f"*ℹ️ INFOS:*\n"
                     f"├ {len(existing_game.players)}/{MAX_PLAYERS} places\n"
                     f"├ 💰 Total des mises: {total_bets} 💵\n"
                     f"└ ⏳ En attente...\n\n"
                     f"📢 Pour rejoindre:\n"
                     f"`/bj + mise`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
            
            # Sauvegarder l'ID du nouveau message
            last_game_message[chat_id] = new_message.message_id
            return
        else:
            error_msg = await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Impossible de rejoindre la partie!"
            )
            await message.delete()
            await asyncio.sleep(3)
            await error_msg.delete()
            return

    # Si on arrive ici, c'est qu'il n'y a pas de partie existante
    # Créer une nouvelle partie
    cleanup_player_games(user.id)
    
    game = MultiPlayerGame(user.id, user.first_name)
    game.initial_chat_id = chat_id
    game.add_player(user.id, bet_amount)
    active_games[user.id] = game
    waiting_games.add(user.id)
    
    # Obtenir le rang du créateur
    emoji, rank_title, _, _ = db.get_player_rank(balance)
    
    # Créer le keyboard markup pour le créateur
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎮 LANCER LA PARTIE", callback_data="start_game")
    ]])
    
    await message.delete()
    
    # Supprimer l'ancien message s'il existe
    if chat_id in last_game_message:
        try:
            await context.bot.delete_message(chat_id, last_game_message[chat_id])
        except:
            pass

    # Envoyer le nouveau message
    sent_message = await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        text="*🎰 NOUVELLE PARTIE*\n"
             "━━━━━━━━━━\n\n"
             "*👥 JOUEURS:*\n"
             f"└ {emoji} {user.first_name} ➜ {bet_amount} 💵\n"
             f"   ├ Rang: {rank_title}\n"
             f"   └ Gains possibles:\n"
             f"      ├ Blackjack: +{int(bet_amount * 2.5)} 💵\n"
             f"      └ Victoire: +{bet_amount * 2} 💵\n\n"
             f"*ℹ️ INFOS:*\n"
             f"├ {len(game.players)}/{MAX_PLAYERS} places\n"
             f"└ ⏳ Expire dans 5 minutes\n\n"
             "📢 Pour rejoindre:\n"
             f"`/bj + mise`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard
    )
    
    # Sauvegarder l'ID du nouveau message
    last_game_message[chat_id] = sent_message.message_id
    
    # Programmer la mise à jour et suppression après 5 minutes
    asyncio.create_task(delete_and_update_game(sent_message, game, context))

async def start_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if user.id not in active_games:
        await update.message.reply_text("❌ Vous n'êtes pas l'hôte d'une partie!")
        return
    
    game = active_games[user.id]
    
    if game.game_status != 'waiting':
        await update.message.reply_text("❌ La partie a déjà commencé!")
        return
    
    if len(game.players) < 1:
        await update.message.reply_text("❌ Il faut au moins 1 joueur pour commencer!")
        return
    
    # Démarrer la partie
    if game.start_game():
        if user.id in waiting_games:
            waiting_games.remove(user.id)
        await display_game(update, context, game)
    else:
        await update.message.reply_text("❌ Impossible de démarrer la partie!")

async def display_game(update: Update, context: ContextTypes.DEFAULT_TYPE, game: MultiPlayerGame):
    chat_id = game.initial_chat_id if hasattr(game, 'initial_chat_id') and game.initial_chat_id else (
        update.callback_query.message.chat_id if update.callback_query
        else update.effective_chat.id
    )
    
    # Définir un THREAD_ID fixe pour tous les messages de jeu
    message_thread_id = 6  # Remplacez 2 par votre THREAD_ID souhaité
    
    # Vérifier et gérer les blackjacks initiaux si nécessaire
    if game.game_status == 'playing':
        all_blackjack = True
        for player_data in game.players.values():
            if game.calculate_hand(player_data['hand']) == 21:
                player_data['status'] = 'blackjack'
            else:
                all_blackjack = False
        if all_blackjack:
            game.game_status = 'finished'
            game.resolve_dealer()
            game.determine_winners()

    current_time = (datetime.utcnow() + timedelta(hours=1)).strftime("%H:%M")

    game_text = (
        "═══『 BLACKJACK 』═══\n\n"
    )
    
    # Croupier
    dealer_cards = ' '.join(str(card) for card in game.dealer_hand)
    dealer_total = game.calculate_hand(game.dealer_hand)
    if game.game_status == 'playing':
        dealer_cards = f"{str(game.dealer_hand[0])} 🎴"
        dealer_total = "?"
    
    game_text += (
        f"👨‍💼 *DEALER* │ {dealer_cards}\n"
        f"├ Total: {dealer_total}\n"
        "──────────────\n\n"
    )
    
    # Joueurs
    for player_id, player_data in game.players.items():
        user = await context.bot.get_chat(player_id)
        hands = [player_data['hand']]
        if 'second_hand' in player_data:
            hands.append(player_data['second_hand'])
        
        for index, hand in enumerate(hands):
            total = game.calculate_hand(hand)
            # Utiliser le bon statut selon la main
            if index == 0:
                status = player_data.get('first_status', player_data['status'])
            else:
                status = player_data.get('second_status', 'playing')
            
            result_text = ""
            status_icon = get_status_emoji(status)

            if status == 'blackjack':
                winnings = int(player_data['bet'] * 2.5)
                result_text = f"+{winnings}"
            elif status == 'win':
                winnings = player_data['bet'] * 2
                result_text = f"+{winnings}"
            elif status == 'lose':
                result_text = f"-{player_data['bet']}"
            elif status == 'push':
                result_text = "±0"
            elif status == 'bust':
                result_text = f"-{player_data['bet']}"

            game_text += (
                f"{status_icon} *{user.first_name}* │ {' '.join(str(card) for card in hand)}\n"
                f"├ Total: {total}\n"
                f"├ Mise: {player_data['bet']} 💵"
            )
            if index == 1:
                game_text += " (Seconde main)"
            if result_text and game.game_status == 'finished':
                game_text += f" │ {result_text}"
            game_text += "\n──────────────\n\n"

    # Résultats finaux
    if game.game_status == 'finished':
        game_text += "*RÉSULTATS*\n"
        for player_id, player_data in game.players.items():
            user = await context.bot.get_chat(player_id)
            first_status = player_data.get('first_status', player_data['status'])
            second_status = player_data.get('second_status', None)
            total_result = 0

            # Calculer le résultat de la première main
            if first_status == 'blackjack':
                total_result += int(player_data['bet'] * 2.5)
            elif first_status == 'win':
                total_result += player_data['bet'] * 2
            elif first_status in ['lose', 'bust']:
                total_result -= player_data['bet']

            # Calculer le résultat de la seconde main
            if second_status:
                if second_status == 'win':
                    total_result += player_data['bet'] * 2
                elif second_status in ['lose', 'bust']:
                    total_result -= player_data['bet']

            # Afficher le résultat total
            if total_result > 0:
                game_text += f"💰 {user.first_name}: *+{total_result}*\n"
            elif total_result < 0:
                game_text += f"💸 {user.first_name}: *{total_result}*\n"
            else:
                game_text += f"🤝 {user.first_name}: *±0*\n"

        game_text += "\n🎮 */bj [mise]* pour rejouer"
    elif current_player_id := game.get_current_player_id():
        player_name = (await context.bot.get_chat(current_player_id)).first_name
        game_text += f"👉 C'est à *{player_name}* de jouer"

    # Footer
    game_text += f"\n\n⌚️ {current_time}"

    # Boutons de jeu
    keyboard = None
    if game.game_status == 'playing' and (current_player_id := game.get_current_player_id()):
        buttons = [
            [
                InlineKeyboardButton("🎯 CARTE", callback_data="hit"),
                InlineKeyboardButton("⏹ STOP", callback_data="stand")
            ]
        ]
        
        # Ajouter le bouton SPLIT uniquement si c'est possible
        if game.can_split(current_player_id):
            buttons.append([
                InlineKeyboardButton("✂️ SPLIT", callback_data="split")
            ])
        
        keyboard = InlineKeyboardMarkup(buttons)

    try:
        if game.game_status == 'finished':
            if update.callback_query:
                try:
                    await update.callback_query.message.delete()
                except Exception:
                    pass
            elif chat_id in game_messages:
                try:
                    await context.bot.delete_message(
                        chat_id=chat_id,
                        message_id=game_messages[chat_id]
                    )
                except Exception:
                    pass

            message = await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,  # Ajout du thread_id ici
                text=game_text,
                parse_mode=ParseMode.MARKDOWN
            )

            host_id = game.host_id
            if host_id in active_games:
                del active_games[host_id]
            if host_id in waiting_games:
                waiting_games.remove(host_id)
            if chat_id in game_messages:
                del game_messages[chat_id]

            end_message = await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,  # Ajout du thread_id ici
                text="🎰 *La partie est terminée!*\n"
                     "Vous pouvez maintenant en démarrer une nouvelle avec `/bj [mise]`",
                parse_mode=ParseMode.MARKDOWN
            )
            last_end_game_message[chat_id] = end_message.message_id
        else:
            if update.callback_query:
                await update.callback_query.message.edit_text(
                    text=game_text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )
            elif chat_id in game_messages:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=game_messages[chat_id],
                    text=game_text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                message = await context.bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=message_thread_id,  # Ajout du thread_id ici
                    text=game_text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN
                )
                game_messages[chat_id] = message.message_id

    except Exception as e:
        print(f"Error in display_game: {e}")

async def cmds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    commands_text = (
        "🎮 *COMMANDES DU BLACKJACK* 🎮\n\n"
        "🎲 *Commandes de jeu:*\n"
        "└─ `/bj [mise]` - Créer une partie\n"
        "└─ `/join [mise]` - Rejoindre la partie\n"
        "📊 *Informations:*\n"
        "└─ `/stats` - Voir vos statistiques\n"
        "└─ `/infos` - Règles du jeu\n"
        "└─ `/cmds` - Liste des commandes\n\n"
        "💰 *Économie:*\n"
        "└─ `/daily` - Bonus quotidien\n\n"
        "💡 *Exemple:*\n"
        "└─ `/bj 100` - Créer une partie avec 100 coins"
    )
    await update.message.reply_text(commands_text, parse_mode=ParseMode.MARKDOWN)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    chat_id = update.effective_chat.id

    if query.data.startswith("admin_"):
        if not is_admin(user.id):
            await query.answer("❌ Action réservée aux administrateurs!", show_alert=True)
            return
            
        _, action, host_id = query.data.split("_")
        host_id = int(host_id)
        
        if host_id not in active_games:
            await query.answer("❌ Cette partie n'existe plus!", show_alert=True)
            return
            
        game = active_games[host_id]

        # Ajouter cette partie pour gérer l'action "cancel"
        if action == "cancel":
            # Supprimer la partie des jeux actifs
            del active_games[host_id]
            if host_id in waiting_games:
                waiting_games.remove(host_id)
                
            # Mettre à jour le message
            await query.message.edit_text(
                "🎮 *MENU ADMIN*\n\n"
                f"✅ Partie de *{game.get_host_name()}* annulée avec succès.",
                parse_mode=ParseMode.MARKDOWN
            )
            await query.answer("✅ Partie annulée!")
            return

    if query.data == "start_game":
        # Vérifier si l'utilisateur est le créateur de la partie
        game = None
        for g in active_games.values():
            if g.host_id == user.id and g.game_status == 'waiting':
                game = g
                break
        
        if not game:
            await query.answer("❌ Vous n'êtes pas le créateur de cette partie!")
            return
            
        if len(game.players) < 1:
            await query.answer("❌ Il faut au moins 1 joueur pour commencer!")
            return
            
        # Démarrer la partie
        if game.start_game():
            if user.id in waiting_games:
                waiting_games.remove(user.id)
            await display_game(update, context, game)
            await query.answer("✅ La partie commence!")
        else:
            await query.answer("❌ Impossible de démarrer la partie!")
        return
    
    # Trouver la partie active
    game = None
    for g in active_games.values():
        if user.id in g.players:
            game = g
            break
    
    if not game:
        await query.answer("❌ Aucune partie trouvée!")
        return
    
    current_player_id = game.get_current_player_id()
    if current_player_id != user.id:
        await query.answer("❌ Ce n'est pas votre tour!")
        return
    
    player_data = game.players[user.id]
    game_ended = False
    
    try:
        if query.data == "hit":
            if 'current_hand' not in player_data:
                player_data['current_hand'] = 'hand'
            current_hand = player_data['current_hand']
            new_card = game.deck.deal()
            player_data[current_hand].append(new_card)
            total = game.calculate_hand(player_data[current_hand])

            if total > 21:
                if current_hand == 'hand' and 'second_hand' in player_data:
                    # Si c'est la première main qui bust et qu'il y a une seconde main
                    player_data['first_status'] = 'bust'  # Marquer la première main comme bust
                    player_data['current_hand'] = 'second_hand'  # Passer à la seconde main
                    player_data['status'] = 'playing'  # Maintenir le statut de jeu
                    await query.answer("💥 Première main bust! Passons à la seconde main.")
                else:
                    # Si c'est la seconde main ou s'il n'y a pas de seconde main
                    if current_hand == 'second_hand':
                        player_data['second_status'] = 'bust'
                    player_data['status'] = 'bust'
                    game_ended = game.next_player()
                    await query.answer("💥 Vous avez dépassé 21!")
            elif total == 21:
                if current_hand == 'hand' and 'second_hand' in player_data:
                    # Si c'est la première main qui fait 21 et qu'il y a une seconde main
                    player_data['first_status'] = 'blackjack'
                    player_data['current_hand'] = 'second_hand'
                    player_data['status'] = 'playing'
                    await query.answer("🌟 Blackjack sur la première main! Passons à la seconde.")
                else:
                    # Si c'est la seconde main ou s'il n'y a pas de seconde main
                    if current_hand == 'second_hand':
                        player_data['second_status'] = 'blackjack'
                    player_data['status'] = 'blackjack'
                    game_ended = game.next_player()
                    await query.answer("🌟 Blackjack!")
            else:
                await query.answer(f"🎯 Total: {total}")
        
        elif query.data == "stand":
            if 'current_hand' not in player_data:
                player_data['current_hand'] = 'hand'
            current_hand = player_data['current_hand']
    
            if current_hand == 'hand' and 'second_hand' in player_data:
                player_data['current_hand'] = 'second_hand'
                player_data['first_status'] = 'stand'  # Stocker le statut de la première main séparément
                player_data['status'] = 'playing'  # Garder le statut principal comme 'playing'
                await query.answer("⏹ Vous restez sur la première main, à la seconde")
            else:
                if current_hand == 'second_hand':
                    player_data['second_status'] = 'stand'
                    player_data['status'] = 'stand'  # Maintenant on peut mettre stand car c'est fini
                else:
                    player_data['status'] = 'stand'
                game_ended = game.next_player()
                await query.answer("⏹ Vous restez")
        
        elif query.data == "split":
            if game.split_hand(user.id):
                player_data['current_hand'] = 'hand'
                await query.answer("✂️ Vous avez splitté votre main!")
            else:
                await query.answer("❌ Impossible de splitter la main!")
        
        # Mise à jour de l'affichage
        await display_game(update, context, game)
        
        # Si la partie est terminée
        if game_ended:
            # Mémoriser tous les joueurs de cette partie
            players_in_game = list(game.players.keys())
            host_id = game.host_id
            
            # Nettoyer toutes les références à la partie
            if host_id in active_games:
                del active_games[host_id]
            if host_id in waiting_games:
                waiting_games.remove(host_id)
            if chat_id in game_messages:
                del game_messages[chat_id]
            
            # Nettoyer chaque joueur des parties actives
            for player_id in players_in_game:
                for game_id in list(active_games.keys()):  # Utiliser une copie de la liste des clés
                    if player_id in active_games[game_id].players:
                        del active_games[game_id]
    
    except Exception as e:
        print(f"Error in button_handler: {e}")
        await query.answer("❌ Une erreur s'est produite!")

async def error_handler(update: Update, context):
    print(f"An error occurred: {context.error}")  # Debug log
    logger.error(f"Update {update} caused error {context.error}")

async def delete_and_update_game(message, game, context, delay_seconds: int = 300):
    """Gère l'expiration d'une partie après un délai"""
    try:
        await asyncio.sleep(delay_seconds)
        
        # Vérifier si la partie est toujours en attente
        host_id = game.host_id
        if host_id in waiting_games:
            # Supprimer la partie des jeux actifs
            if host_id in active_games:
                del active_games[host_id]
            waiting_games.remove(host_id)
            
            # Préparer le message d'expiration
            expired_message = (
                "*🎰 PARTIE EXPIRÉE*\n"
                "━━━━━━━━━━\n\n"
                f"❌ Cette partie a expiré après 5 minutes\n"
                f"👤 Créée par: *{game.get_host_name()}*\n"
                f"💰 Mise: *{game.get_bet()}* coins\n"
                f"⏰ Status: Annulée (temps écoulé)"
            )
            
            # Essayer d'envoyer un nouveau message si la mise à jour échoue
            try:
                await message.edit_text(
                    expired_message,
                    message_thread_id=message_thread_id,
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as edit_error:
                # Si la mise à jour échoue, essayer d'envoyer un nouveau message
                try:
                    chat_id = message.chat_id
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=expired_message,
                        message_thread_id=message_thread_id,
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as send_error:
                    print(f"Impossible d'envoyer le message d'expiration: {send_error}")
                    
    except Exception as e:
        print(f"Erreur dans delete_and_update_game: {e}")

async def infos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🎰 *BLACKJACK - RÈGLES DU JEU* 🎰\n\n"
        "*🎯 Objectif:*\n"
        "└─ Obtenir un total plus proche de 21 que le croupier\n"
        "└─ Ne pas dépasser 21\n\n"
        "*🃏 Valeurs des cartes:*\n"
        "└─ As ➜ 1 ou 11\n"
        "└─ Roi, Dame, Valet ➜ 10\n"
        "└─ Autres cartes ➜ Valeur faciale\n\n"
        "*💰 Gains:*\n"
        "└─ Blackjack (21) ➜ x2.5\n"
        "└─ Victoire ➜ x2\n"
        "└─ Égalité ➜ Mise remboursée\n\n"
        "*📌 Limites:*\n"
        "└─ Mise min: 10 coins\n"
        "└─ Mise max: 1000000 coins\n"
        f"└─ {MAX_PLAYERS} joueurs maximum\n\n"
        "*⚡️ Actions en jeu:*\n"
        "└─ 🎯 CARTE - Tirer une carte\n"
        "└─ ⏹ RESTER - Garder sa main"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche les statistiques du joueur"""
    user = update.effective_user
    balance = db.get_balance(user.id)
    
    emoji, rank_title, progress, next_rank = get_player_rank(balance)
    
    # Crée une barre de progression
    progress_bar_length = 10
    filled_length = int(progress_bar_length * progress / 100)
    progress_bar = "█" * filled_length + "░" * (progress_bar_length - filled_length)
    
    stats_text = (
        f"*STATISTIQUES DE {user.first_name}*\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"💵 *Solde:* {balance:,} $\n"
        f"🎖️ *Rang:* {emoji} {rank_title}\n"
    )
    
    if next_rank:
        stats_text += (
            f"\n*Progression vers {next_rank}*\n"
            f"[{progress_bar}] {progress:.1f}%\n"
        )
    else:
        stats_text += "\n🏆 *Rang Maximum Atteint !*\n"
    
    # Ajoute des statistiques de jeu si tu en as
    games_played = db.get_games_played(user.id)  # Tu devras créer cette fonction
    wins = db.get_wins(user.id)  # Tu devras créer cette fonction
    
    if games_played:
        win_rate = (wins / games_played) * 100
        stats_text += (
            f"\n📊 *Statistiques de Jeu*\n"
            f"├ Parties jouées: {games_played}\n"
            f"├ Victoires: {wins}\n"
            f"└ Taux de victoire: {win_rate:.1f}%\n"
        )
    
    await update.message.reply_text(
        stats_text,
        parse_mode=ParseMode.MARKDOWN
    )

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu d'administration pour les admins"""
    user = update.effective_user
    
    if not is_admin(user.id):
        await update.message.reply_text("❌ Cette commande est réservée aux administrateurs.")
        return
        
    # Trouver toutes les parties en attente
    waiting_games_list = []
    for host_id, game in active_games.items():
        if game.game_status == 'waiting':
            host_name = game.get_host_name()
            players_count = len(game.players)
            total_bet = sum(p['bet'] for p in game.players.values())
            waiting_games_list.append((host_id, host_name, players_count, total_bet))
    
    if not waiting_games_list:
        await update.message.reply_text(
            "🎮 *MENU ADMIN*\n\n"
            "Aucune partie en attente.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
        
    # Créer les boutons pour chaque partie
    keyboard = []
    for host_id, host_name, players_count, total_bet in waiting_games_list:
        # Bouton pour forcer le démarrage
        start_button = InlineKeyboardButton(
            f"▶️ Start",
            callback_data=f"admin_start_{host_id}"
        )
        # Bouton pour annuler
        cancel_button = InlineKeyboardButton(
            f"❌ Cancel",
            callback_data=f"admin_cancel_{host_id}"
        )
        keyboard.append([start_button, cancel_button])
    
    message = "🎮 *MENU ADMIN*\n\n*Parties en attente:*\n\n"
    
    for host_id, host_name, players_count, total_bet in waiting_games_list:
        message += (
            f"👤 Hôte: *{host_name}*\n"
            f"├ ID: `{host_id}`\n"
            f"├ Joueurs: {players_count}\n"
            f"└ Total mises: {total_bet} 💵\n\n"
        )
    
    await update.message.reply_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def rangs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche tous les rangs disponibles"""
    ranks = [
        (0, "🤡 Clochard du Casino"),
        (500, "🎲 Joueur Amateur"),
        (1000, "🎰 Joueur Lambda"),
        (2500, "💰 Petit Parieur"),
        (5000, "💎 Parieur Régulier"),
        (10000, "🎩 High Roller"),
        (25000, "👑 Roi du Casino"),
        (50000, "🌟 VIP Diamond"),
        (100000, "🔥 Parieur Fou"),
        (250000, "🌈 Légende du Casino"),
        (500000, "⚡ Master des Tables"),
        (1000000, "🌌 Empereur du Gambling")
    ]
    
    text = "*📊 RANGS DU CASINO 📊*\n━━━━━━━━━━━━━━━\n\n"
    
    # Afficher tous les rangs
    for threshold, rank in ranks:
        text += f"*{rank}*\n└ Requis: {threshold:,} $\n\n"
    
    # Ajouter le rang actuel du joueur
    user_balance = db.get_balance(update.effective_user.id)
    emoji, rank_title, progress, next_rank = db.get_player_rank(user_balance)
    
    text += (
        f"\n*Votre rang actuel:*\n"
        f"└ {emoji} {rank_title}\n"
    )
    
    if next_rank:
        # Créer une barre de progression
        progress_bar_length = 10
        filled_length = int(progress_bar_length * progress / 100)
        progress_bar = "█" * filled_length + "░" * (progress_bar_length - filled_length)
        
        text += (
            f"\n*Progression vers {next_rank}*\n"
            f"[{progress_bar}] {progress:.1f}%\n"
        )
    else:
        text += "\n🏆 *Rang Maximum Atteint !*"
    
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le solde et les informations bancaires du joueur"""
    user = update.effective_user

    if await handle_forbidden_thread(update.message): return
    
    # Vérifier si l'utilisateur existe
    if not db.user_exists(user.id):
        await update.message.reply_text("❌ Vous devez d'abord utiliser /start pour vous inscrire !")
        return
    
    # Récupérer les stats du joueur
    stats = db.get_stats(user.id)
    emoji, title, progress, next_rank = db.get_player_rank(stats['balance'])
    
    # Créer le message avec les informations bancaires
    bank_message = (
        f"🏦 Informations bancaires de {user.first_name}\n\n"
        f"💰 Solde : {stats['balance']} coins\n"
        f"{emoji} Rang : {title}\n"
    )
    
    # Ajouter la progression vers le prochain rang si disponible
    if next_rank:
        bank_message += f"📈 Progression : {progress:.1f}% vers {next_rank}"

    await update.message.reply_text(bank_message)

async def check_game_timeouts(context: ContextTypes.DEFAULT_TYPE):
    games_to_check = list(active_games.items())  # Créer une copie
    for game_id, game in games_to_check:
        if game.game_status == 'playing':
            current_player_id = game.get_current_player_id()
            if current_player_id and game.check_timeout():
                try:
                    player_data = game.players[current_player_id]
                    if player_data['status'] == 'playing':
                        player_data['status'] = 'stand'
                        game_ended = game.next_player()
                        game.last_action_time = datetime.utcnow()

                        if game_ended:
                            game.game_status = 'finished'
                            game.resolve_dealer()
                            game.determine_winners()
                        
                        # Créer un faux update pour display_game
                        dummy_update = Update(0, None)
                        await display_game(dummy_update, context, game)

                except Exception as e:
                    print(f"Erreur dans check_game_timeouts: {e}")

async def update_classement_job(context: ContextTypes.DEFAULT_TYPE):
    """Met à jour automatiquement le classement"""
    if CLASSEMENT_MESSAGE_ID is not None and CLASSEMENT_CHAT_ID is not None:
        cursor = db.conn.cursor()
        cursor.execute("""
            SELECT username, balance 
            FROM users 
            ORDER BY balance DESC 
            LIMIT 50
        """)
        
        rankings = cursor.fetchall()
        current_time = (datetime.utcnow() + timedelta(hours=1)).strftime("%H:%M")

        
        message = (
            "🎯 *CLASSEMENT* 🎯\n"
            "━━━━━━━━━━━━━━━\n\n"
        )
        
        for i, (username, balance) in enumerate(rankings, 1):
            emoji, rank_title, _, _ = db.get_player_rank(balance)
            
            if i == 1:
                medal = "👑"
            elif i == 2:
                medal = "🥈"
            elif i == 3:
                medal = "🥉"
            elif i <= 10:
                medal = "⭐"
            else:
                medal = "•"
                
            message += (
                f"{medal} *#{i}* {emoji} *{username}*\n"
                f"├ {rank_title}\n"
                f"└ {balance:,} 💵\n"
            )
            
            if i in [3, 10]:
                message += "━━━━━━━━━━━━━━━\n"
            else:
                message += "\n"
                
        message += f"\n⌚️ Mis à jour: {current_time}"
        
        try:
            await context.bot.edit_message_text(
                chat_id=CLASSEMENT_CHAT_ID,
                message_id=CLASSEMENT_MESSAGE_ID,
                text=message,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Erreur mise à jour classement: {e}")

async def classement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Affiche un classement des 50 meilleurs joueurs
    Se met à jour automatiquement toutes les 5 minutes
    """
    global CLASSEMENT_MESSAGE_ID, CLASSEMENT_CHAT_ID
    
    # Vérifier si le classement existe déjà
    if CLASSEMENT_MESSAGE_ID is not None:
        try:
            await update.message.reply_text(
                "❌ Le classement est déjà actif dans un autre chat.\n"
                "Il ne peut être affiché qu'à un seul endroit à la fois."
            )
        except Exception:
            pass
        return
    
    cursor = db.conn.cursor()
    cursor.execute("""
        SELECT username, balance 
        FROM users 
        ORDER BY balance DESC 
        LIMIT 50
    """)
    
    rankings = cursor.fetchall()
    
    # Corriger l'heure pour qu'elle soit à l'heure française
    current_time = (datetime.utcnow() + timedelta(hours=1)).strftime("%H:%M")

    
    message = (
        "🎯 *CLASSEMENT* 🎯\n"
        "━━━━━━━━━━━━━━━\n\n"
    )
    
    for i, (username, balance) in enumerate(rankings, 1):
        # Obtenir le rang du joueur avec son emoji et son titre
        emoji, rank_title, _, _ = db.get_player_rank(balance)
        
        # Médailles pour le podium
        if i == 1:
            medal = "👑"
        elif i == 2:
            medal = "🥈"
        elif i == 3:
            medal = "🥉"
        elif i <= 10:
            medal = "⭐"
        else:
            medal = "•"
            
        # Formater chaque entrée avec le nom du rang
        message += (
            f"{medal} *#{i}* {emoji} *{username}*\n"
            f"├ {rank_title}\n"
            f"└ {balance:,} 💵\n"
        )
        
        # Ajouter un séparateur après le podium et top 10
        if i in [3, 10]:
            message += "━━━━━━━━━━━━━━━\n"
        else:
            message += "\n"
            
    # Ajouter le timestamp de mise à jour
    message += f"\n⌚️ Mis à jour: {current_time}"
    
    try:
        # Si c'est une nouvelle commande (pas une mise à jour automatique)
        if update and update.message:
            # Vérifier si c'est un supergroupe
            if update.effective_chat.type == "supergroup":
                # Envoyer le nouveau message de classement
                sent_message = await update.message.reply_text(
                    message,
                    parse_mode=ParseMode.MARKDOWN
                )
                CLASSEMENT_MESSAGE_ID = sent_message.message_id
                CLASSEMENT_CHAT_ID = update.effective_chat.id
            else:
                await update.message.reply_text(
                    "❌ Cette commande doit être utilisée dans un supergroupe pour fonctionner correctement."
                )
        # Si c'est une mise à jour automatique
        elif CLASSEMENT_MESSAGE_ID and CLASSEMENT_CHAT_ID:
            await context.bot.edit_message_text(
                chat_id=CLASSEMENT_CHAT_ID,
                message_id=CLASSEMENT_MESSAGE_ID,
                text=message,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error(f"Erreur mise à jour classement: {e}")

async def reset_classement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande admin pour réinitialiser le classement"""
    global CLASSEMENT_MESSAGE_ID, CLASSEMENT_CHAT_ID
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Cette commande est réservée aux administrateurs.")
        return
        
    CLASSEMENT_MESSAGE_ID = None
    CLASSEMENT_CHAT_ID = None
    await update.message.reply_text("✅ Le classement a été réinitialisé.")

def main():
    try:
        defaults = Defaults(parse_mode=ParseMode.MARKDOWN)
        application = (
            Application.builder()
            .token(TOKEN)
            .defaults(defaults)
            .build()
        )

        # Vos handlers existants
        application.add_handler(CommandHandler("classement", classement))
        application.add_handler(CommandHandler("admin", admin_menu))
        application.add_handler(CommandHandler('bank', cmd_bank))
        application.add_handler(CommandHandler("start", cmd_start))
        application.add_handler(CommandHandler("infos", infos))
        application.add_handler(CommandHandler("rangs", rangs))
        application.add_handler(CommandHandler("cmds", cmds))
        application.add_handler(CommandHandler("stats", stats))
        application.add_handler(CommandHandler("daily", daily))
        application.add_handler(CommandHandler("bj", create_game))
        application.add_handler(CommandHandler("setcredits", set_credits))
        application.add_handler(CommandHandler("addcredits", add_credits))
        application.add_handler(CommandHandler("reset_classement", reset_classement))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.add_error_handler(error_handler)
        application.job_queue.run_repeating(check_game_timeouts, interval=5)  # Vérifie toutes les 5 secondes
        application.job_queue.run_repeating(update_classement_job, interval=300)  # 300 secondes = 5 minutes
   
        print("🎲 Blackjack Bot démarré !")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"Erreur critique: {e}")
        raise
    finally:
        db.conn.close()

if __name__ == '__main__':
    main()
