import argparse
import chess
import chess.pgn

MAX_VISIT_GAMES = 100000

parser = argparse.ArgumentParser(description='Filter PGN file')
parser.add_argument('--inf', help="Specify input file")
parser.add_argument('--outf', help="Specify output file")
parser.add_argument('--minrtg', help="Specify minimum rating")

args = parser.parse_args()

print(args)

MIN_RTG = 1500

if args.minrtg:
	MIN_RTG = int(args.minrtg)

pgn = open(args.inf)

cnt=0

found=0

pgns = []

class HeaderVisitor(chess.pgn.BaseVisitor):
	def __init__(self):
		self.game = chess.pgn.Game()		

		self.variation_stack = [self.game]
		self.starting_comment = ""
		self.in_variation = False

	def visit_header(self, tagname, tagvalue):
		self.game.headers[tagname] = tagvalue

	def result(self):
		return self.game

while cnt<MAX_VISIT_GAMES:
	rawgame = chess.pgn.read_game(pgn)
	if rawgame==None:
		break		
	white_elo = 1500
	black_elo = 1500

	try:
		white_elo = int(rawgame.headers["WhiteElo"])
		black_elo = int(rawgame.headers["BlackElo"])
	except:
		pass

	cnt+=1

	if white_elo >= MIN_RTG and black_elo >= MIN_RTG:
		found+=1
		print("{:5d} {:5d}".format(cnt,found))

		exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
		pgn_string = rawgame.accept(exporter)

		pgns.append(pgn_string)

with open(args.outf,"w") as outfile:
	outfile.write("\n\n\n".join(pgns)+"\n\n\n")
