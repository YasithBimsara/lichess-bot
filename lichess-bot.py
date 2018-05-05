import argparse
import chess
from chess.variant import find_variant
import chess.polyglot
import engine_wrapper
import model
import json
import lichess
import logging
import multiprocessing
import traceback
import logging_pool
import backoff
from config import load_config
from conversation import Conversation, ChatLine
from functools import partial
from requests.exceptions import ChunkedEncodingError, ConnectionError, HTTPError
from urllib3.exceptions import ProtocolError
import threading
import time

try:
    from http.client import RemoteDisconnected
    # New in version 3.5: Previously, BadStatusLine('') was raised.
except ImportError:
    from http.client import BadStatusLine as RemoteDisconnected

__version__ = "1.0.0-rc.1"

MATE_SCORE = 10000
RESIGN_SCORE = -9997
WON_SCORE = 9997
TIME_TROUBLE_LIMIT = 3000

def upgrade_account(li):
    if li.upgrade_to_bot_account() is None:
        return False

    print("Succesfully upgraded to Bot Account!")
    return True

@backoff.on_exception(backoff.expo, BaseException, max_time=600)
def watch_control_stream(control_queue, li):
    for evnt in li.get_event_stream().iter_lines():
        if evnt:
            event = json.loads(evnt.decode('utf-8'))
            control_queue.put_nowait(event)
        else:
            control_queue.put_nowait({"type": "ping"})

def start(li, user_profile, engine_factory, config):
    # init
    max_games = config["max_concurrent_games"]
    print("You're now connected to {} and awaiting challenges.".format(config["url"]))
    manager = multiprocessing.Manager()
    challenge_queue = []
    control_queue = manager.Queue()
    control_stream = multiprocessing.Process(target=watch_control_stream, args=[control_queue, li])
    control_stream.start()
    busy_processes = 0
    queued_processes = 0
    challenge_config = config["challenge"]

    with logging_pool.LoggingPool(max_games+1) as pool:
        while True:
            event = control_queue.get()
            if event["type"] == "local_game_done":
                busy_processes -= 1
                print("+++ Process Free. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))
            elif event["type"] == "challenge":
                chlng = model.Challenge(event["challenge"])
                if chlng.is_supported(challenge_config):
                    challenge_queue.append(chlng)
                    if (challenge_config.get("sort_by", "best") == "best"):
                        challenge_queue.sort(key=lambda c: -c.score())
                else:
                    try:
                        li.decline_challenge(chlng.id)
                        print("    Decline {}".format(chlng))
                    except HTTPError as exception:
                        if exception.response.status_code != 404: # ignore missing challenge
                            raise exception
            elif event["type"] == "gameStart":
                if queued_processes <= 0:
                    print("Something went wrong. Game is starting and we don't have a queued process")
                else:
                    queued_processes -= 1
                game_id = event["game"]["id"]
                pool.apply_async(play_game, [li, game_id, control_queue, engine_factory, user_profile, config])
                busy_processes += 1
                print("--- Process Used. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))
            while ((queued_processes + busy_processes) < max_games and challenge_queue): # keep processing the queue until empty or max_games is reached
                chlng = challenge_queue.pop(0)
                try:
                    response = li.accept_challenge(chlng.id)
                    print("    Accept {}".format(chlng))
                    queued_processes += 1
                    print("--- Process Queue. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))
                except HTTPError as exception:
                    if exception.response.status_code == 404: # ignore missing challenge
                        print("    Skip missing {}".format(chlng))
                    else:
                        raise exception

    control_stream.terminate()
    control_stream.join()

def game_chat(li,game_id,text,public=False):
    li.chat(game_id,"player",text)
    if public:
        li.chat(game_id,"spectator",text)

watch_engine = {}

@backoff.on_exception(backoff.expo, BaseException, max_time=600)
def play_game(li, game_id, control_queue, engine_factory, user_profile, config):
    #game state
    gg_said = False

    updates = li.get_game_stream(game_id).iter_lines()

    #Initial response of stream will be the full game info. Store it
    game = model.Game(json.loads(next(updates).decode('utf-8')), user_profile["username"], li.baseUrl, config.get("abort_time", 20))
    board = setup_board(game)
    engine = engine_factory(board)
    conversation = Conversation(game, engine, li, __version__)

    print("+++ {}".format(game))

    engine_cfg = config["engine"]
    polyglot_cfg = engine_cfg.get("polyglot", {})

    if not polyglot_cfg.get("enabled") or not play_first_book_move(game, engine, board, li, config):
        play_first_move(game, engine, board, li)

    engine.set_time_control(game)

    save_fen(config,board)

    if board.fullmove_number == 1:        
        cnt = 0
        glms = config["good_luck_messages"]
        for glm in glms:
            game_chat(li,game.id,glm, public = ( cnt < ( len(glms) - 1 ) ) )
            cnt+=1

    watch_engine[game.id] = True
    def print_engine_info(engine, game):
        global watch_engine
        while watch_engine[game.id]:            
            try:
            	if not engine.engine.idle:
                    info = engine.engine.info_handlers[0].info
                    score = info["score"][1]
                    print("{} - {}\n  {:8s} {:8s}".format(game.white, game.black, str(score.cp), str(score.mate))," ".join(list(map(chess.Move.uci,info["pv"][1][0:5]))))
            except:
                pass
            time.sleep(1)
        print("engine watch finished")

    th = threading.Thread(target = print_engine_info, args = (engine, game))
    th.start()

    try:
        for binary_chunk in updates:
            upd = json.loads(binary_chunk.decode('utf-8')) if binary_chunk else None
            u_type = upd["type"] if upd else "ping"
            if u_type == "chatLine":
                conversation.react(ChatLine(upd), game)
            elif u_type == "gameState":
                game.state = upd
                moves = upd["moves"].split()
                board = update_board(board, moves[-1])

                save_fen(config, board)
                
                if is_engine_move(game, moves):
                    best_move = None

                    pos_eval = 0
                    best_move = get_book_move(board, config)

                    if best_move == None:
                        print("searching for move")
                        best_move = engine.search(board, upd["wtime"], upd["btime"], upd["winc"], upd["binc"])
                        info=engine.engine.info_handlers[0].info
                        score=info["score"][1]                        
                        if score[1] == None:
                            pos_eval = score[0]
                        else:
                            mate = score[1]
                            if mate > 0:
                            	pos_eval = MATE_SCORE - mate
                            else:
                                pos_eval = -MATE_SCORE - mate
                        print("best move",best_move,pos_eval)
                    else:
                        pass

                    opp_time = int(upd["wtime"])

                    if game.is_white:
                        opp_time = int(upd["btime"])

                    is_inc = ( int(upd["winc"]) > 0 ) or ( int(upd["binc"]) > 0 )

                    if ( pos_eval > RESIGN_SCORE ) or ( ( opp_time < TIME_TROUBLE_LIMIT ) and ( not is_inc ) ):
                        if pos_eval > WON_SCORE and not gg_said:                        	
                            ggm = config["good_game_message"]
                            if not ggm is None:
                                game_chat(li,game.id, ggm, public=True)
                            gg_said = True
                        li.make_move(game.id, best_move)
                        game.abort_in(config.get("abort_time", 20))
                    else:
                        print("resign")
                        game_chat(li,game.id,"good game",public=True)
                        li.resign_hopeless_game(game.id)                        
            elif u_type == "ping":
                if game.should_abort_now():
                    print("    Aborting {} by lack of activity".format(game.url()))
                    li.abort(game.id)
    except (RemoteDisconnected, ChunkedEncodingError, ConnectionError, ProtocolError, HTTPError) as exception:
        print("Abandoning game due to connection error")
        traceback.print_exception(type(exception), exception, exception.__traceback__)
    finally:
        print("--- {} Game over".format(game.url()))
        engine.quit()
        watch_engine[game.id] = False
        # This can raise queue.NoFull, but that should only happen if we're not processing
        # events fast enough and in this case I believe the exception should be raised
        control_queue.put_nowait({"type": "local_game_done"})

def save_fen(config, board):
    if not config["save_fen"] == None:
        fen = board.fen()
        with open(config["save_fen"],"w") as outfile:
            outfile.write(fen)

def play_first_move(game, engine, board, li):        
    moves = game.state["moves"].split()
    if is_engine_move(game, moves):    	
        # need to hardcode first movetime since Lichess has 30 sec limit.        
        best_move = engine.first_search(board, 10000)
        li.make_move(game.id, best_move)
        return True
    return False


def play_first_book_move(game, engine, board, li, config):    
    moves = game.state["moves"].split()
    if is_engine_move(game, moves):
        print("playing first book move")
        book_move = get_book_move(board, config)
        if not book_move is None:
            li.make_move(game.id, book_move)
            return True
        else:
            print("falling back to engine move")
            return play_first_move(game, engine, board, li)
    return False

def get_book_move(board, config):
    if not "polyglot" in config["engine"]:
        return None

    if not config["engine"]["polyglot"]["enabled"]:
        return None

    for book_path in config["engine"]["polyglot"]["books"]:        
        book_move=get_book_move_from_book(board, book_path)
        if not (book_move is None):
            print("move found in", book_path, book_move)
            return book_move

    return None

def get_book_move_from_book(board, book_name):	
    try:
        with chess.polyglot.open_reader(book_name) as reader:
            book_move = reader.weighted_choice(board).move()
            return book_move
    except:
        pass

    return None

def setup_board(game):
    if game.variant_name.lower() == "chess960":
        board = chess.Board(game.initial_fen, chess960=True)
    elif game.variant_name == "From Position":
        board = chess.Board(game.initial_fen)
    else:
        VariantBoard = find_variant(game.variant_name)
        board = VariantBoard()
    moves = game.state["moves"].split()
    for move in moves:
        board = update_board(board, move)

    return board


def is_white_to_move(game, moves):
    return len(moves) % 2 == (0 if game.white_starts else 1)


def is_engine_move(game, moves):
    return game.is_white == is_white_to_move(game, moves)


def update_board(board, move):
    uci_move = chess.Move.from_uci(move)
    board.push(uci_move)
    return board

def intro():
    return r"""
.   _/|
.  // o\
.  || ._)  lichess-bot %s
.  //__\
.  )___(   Play on Lichess with a bot
""".lstrip() % __version__

if __name__ == "__main__":
    print(intro())
    parser = argparse.ArgumentParser(description='Play on Lichess with a bot')
    parser.add_argument('-u', action='store_true', help='Add this flag to upgrade your account to a bot account.')
    parser.add_argument('-v', action='store_true', help='Verbose output. Changes log level from INFO to DEBUG.')
    parser.add_argument('--config', help="Specify a configuration file name - without .yml extension! ( defaults to: config )")

    args = parser.parse_args()

    logger = logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO)

    CONFIG = load_config(args.config or "config")

    li = lichess.Lichess(CONFIG["token"], CONFIG["url"], __version__)

    user_profile = li.get_profile()
    username = user_profile["username"]
    is_bot = user_profile.get("title") == "BOT"
    print("Welcome {}!".format(username))

    if args.u is True and is_bot is False:
        is_bot = upgrade_account(li)

    if is_bot:
        engine_factory = partial(engine_wrapper.create_engine, CONFIG)
        start(li, user_profile, engine_factory, CONFIG)
    else:
        print("{} is not a bot account. Please upgrade your it to a bot account!".format(user_profile["username"]))
