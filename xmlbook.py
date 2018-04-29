import xml.etree.ElementTree as ET
import chess
import chess.polyglot
import sys

ROOT = None

ANNOT_WEIGHTS = {
	"!!" : 500,
	"!"  : 250,
	"!?" : 100,
	"-"  : 50,
	"?!" : 10
}

def get_zobrist_key_hex(board):
	return "%0.16x" % chess.polyglot.zobrist_hash(board)

def xml_path(name):
	return "engines/"+name+".xml"

def bin_path(name):
	return "engines/"+name+".bin"

def load(name="default"):
	global ROOT
	try:		
		path = xml_path(name)
		#print("loading xml book {}".format(path))
		tree = ET.parse(path)
		ROOT = tree.getroot()
	except:
		pass

def convert(name="default"):
	load(name)
	board=chess.Board()
	allentries=[]

	cnt=0

	for position in ROOT[0]:
		fen = position.attrib["tfen"]+" 1 0"
		board.set_fen(fen)
		zobrist_key_hex=get_zobrist_key_hex(board)
		#print("{:<5d} {} {}\n{}".format(cnt+1,zobrist_key_hex,fen,board))
		zbytes=bytes.fromhex(zobrist_key_hex)
		for movelist in position:
			for move in movelist:
				if move.tag=="move":
					san=move.attrib["s"]
					annot=move[1].text
					weight=0
					if not annot==None:
						try:
							weight=ANNOT_WEIGHTS[annot]
						except:
							pass
					else:
						annot="none"				
					#print("{:8} {:4} {:4}".format(san,annot,weight))
					try:
						m=board.parse_san(san)
						mi=m.to_square+(m.from_square << 6)					
						if not m.promotion==None:
							mi+=((m.promotion-1) << 12)
						mbytes=bytes.fromhex("%0.4x" % mi)										
						wbytes=bytes.fromhex("%0.4x" % weight)					
						lbytes=bytes.fromhex("%0.8x" % 0)
						allbytes=zbytes+mbytes+wbytes+lbytes
						if weight>0:
							allentries.append(allbytes)
					except:
						#print("parsing error")
						pass
		cnt+=1

	sorted_weights=sorted(allentries,key=lambda entry:entry[10:12],reverse=True)
	sorted_entries=sorted(sorted_weights,key=lambda entry:entry[0:8])

	print("total of {} moves added to book {}".format(len(allentries),bin_path(name)))
	with open(bin_path(name), 'wb') as outfile:
		for entry in sorted_entries:
			outfile.write(entry)						

name="default"

if len(sys.argv)>1:
	name=sys.argv[1]

print("converting {}".format(name))

convert(name)