"""Places where an air ambulance landing is routine (bases, major hospitals, airports).

A HEMS aircraft that lands or drops off radar low *away* from all of these is
treated as a probable on-scene landing. Coordinates are approximate; the
exclusion radius (HEMS_EXCLUDE_KM) absorbs the error.
"""

HEMS_EXCLUDE_KM = 2.5

HEMS_SITES: list[tuple[str, float, float]] = [
    # Air ambulance bases
    ("Midlands AA RAF Cosford", 52.640, -2.306), ("Midlands AA Strensham", 52.064, -2.142),
    ("Midlands AA Tatenhill", 52.814, -1.763), ("GWAAC Filton", 51.523, -2.594),
    ("Cornwall AA Newquay", 50.440, -4.995), ("Devon AA Exeter Airport", 50.734, -3.414),
    ("Devon AA Eaglescott", 50.928, -3.989), ("Dorset & Somerset AA Henstridge", 50.986, -2.360),
    ("Wiltshire AA Semington", 51.338, -2.157), ("Thames Valley AA RAF Benson", 51.616, -1.096),
    ("Hants & IoW AA Thruxton", 51.210, -1.600), ("KSS AA Redhill", 51.214, -0.139),
    ("KSS AA Marden", 51.188, 0.475), ("London AA Royal London", 51.519, -0.059),
    ("EHAAT North Weald", 51.721, 0.154), ("EHAAT Earls Colne", 51.914, 0.683),
    ("Magpas RAF Wyton", 52.357, -0.108), ("EAAA Norwich", 52.676, 1.283),
    ("EAAA Cambridge", 52.205, 0.175), ("LNAA Bardney", 53.214, -0.334),
    ("DLRAA East Midlands Airport", 52.831, -1.328), ("TWAA Coventry Airport", 52.370, -1.480),
    ("Yorkshire AA Nostell", 53.643, -1.400), ("Yorkshire AA Topcliffe", 54.205, -1.382),
    ("Great North AA Teesside", 54.509, -1.429), ("Great North AA Langwathby", 54.690, -2.660),
    ("NWAA Barton", 53.472, -2.388), ("NWAA Blackpool", 53.772, -3.029),
    ("Wales AA Welshpool", 52.628, -3.153), ("Wales AA Caernarfon", 53.102, -4.338),
    ("Wales AA Dafen", 51.690, -4.120), ("Wales AA Cardiff Heliport", 51.469, -3.159),
    ("Scottish AA Perth", 56.439, -3.372), ("Scottish AA Aberdeen", 57.200, -2.200),
    ("ScotAmb Glasgow", 55.860, -4.287), ("ScotAmb Inverness", 57.542, -4.048),
    ("NI AA Maze Long Kesh", 54.517, -6.120),
    # Major hospitals with helipads
    ("Southmead Bristol", 51.497, -2.592), ("Derriford Plymouth", 50.417, -4.113),
    ("Royal Devon Exeter", 50.717, -3.507), ("Torbay Hospital", 50.483, -3.547),
    ("Gloucestershire Royal", 51.866, -2.230), ("Cheltenham General", 51.893, -2.071),
    ("QE Birmingham", 52.452, -1.943), ("UHCW Coventry", 52.421, -1.441),
    ("Royal Stoke", 52.997, -2.211), ("Southampton General", 50.933, -1.434),
    ("John Radcliffe Oxford", 51.764, -1.218), ("Addenbrooke's", 52.175, 0.140),
    ("St George's Tooting", 51.427, -0.175), ("King's College Hospital", 51.468, -0.094),
    ("Salford Royal", 53.488, -2.322), ("Aintree", 53.467, -2.934),
    ("Leeds General", 53.802, -1.552), ("Hull Royal", 53.744, -0.357),
    ("Northern General Sheffield", 53.410, -1.457), ("RVI Newcastle", 54.980, -1.620),
    ("James Cook Middlesbrough", 54.551, -1.214), ("QMC Nottingham", 52.944, -1.186),
    ("Leicester Royal", 52.627, -1.136), ("Royal Cornwall Truro", 50.267, -5.093),
    ("Morriston Swansea", 51.676, -3.932), ("UHW Cardiff", 51.508, -3.190),
    ("QEUH Glasgow", 55.862, -4.339), ("Royal Infirmary Edinburgh", 55.922, -3.135),
    ("Aberdeen Royal", 57.154, -2.135), ("Ninewells Dundee", 56.465, -3.037),
    ("Raigmore Inverness", 57.474, -4.192), ("Royal Victoria Belfast", 54.595, -5.954),
    ("Royal Sussex Brighton", 50.819, -0.118), ("Musgrove Taunton", 51.012, -3.121),
    ("Yeovil District", 50.945, -2.634), ("Dorset County", 50.712, -2.443),
    ("Poole Hospital", 50.721, -1.975), ("Great Western Swindon", 51.537, -1.731),
    ("Hereford County", 52.058, -2.709), ("Worcestershire Royal", 52.192, -2.192),
    ("Royal Shrewsbury", 52.709, -2.792), ("North Devon Barnstaple", 51.091, -4.050),
    ("Norfolk & Norwich", 52.617, 1.218), ("Royal United Bath", 51.392, -2.392),
    # Airports / airfields used for fuel
    ("Gloucestershire Airport", 51.894, -2.167), ("Bristol Airport", 51.383, -2.719),
    ("Cardiff Airport", 51.397, -3.343), ("Birmingham Airport", 52.454, -1.748),
    ("Manchester Airport", 53.354, -2.275), ("Southampton Airport", 50.950, -1.357),
]
