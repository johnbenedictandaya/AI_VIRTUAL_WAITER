-- init_restaurant_db.sql : Perpetual Bistro schema + seed data (parody fast-food menu)
-- Bump user_version whenever the schema or menu changes: db_tools.init_db() rebuilds old databases automatically.
PRAGMA foreign_keys = ON;
PRAGMA user_version = 2;

CREATE TABLE IF NOT EXISTS menu_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    category   TEXT    NOT NULL,
    price      REAL    NOT NULL CHECK (price >= 0),
    stock_qty  INTEGER NOT NULL DEFAULT 0 CHECK (stock_qty >= 0),
    allergens  TEXT    NOT NULL DEFAULT '',          -- comma-separated, lowercase
    aliases    TEXT    NOT NULL DEFAULT ''           -- nicknames customers actually say, comma-separated
);

CREATE TABLE IF NOT EXISTS table_sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_no   INTEGER NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'OPEN'
               CHECK (status IN ('OPEN', 'CONFIRMED', 'CANCELLED')),
    created_at TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_line_items (
    line_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id           INTEGER NOT NULL REFERENCES table_sessions(session_id),
    item_id              INTEGER NOT NULL REFERENCES menu_items(id),
    quantity             INTEGER NOT NULL CHECK (quantity > 0),
    special_instructions TEXT    NOT NULL DEFAULT ''
);

-- Discounts (added without bumping user_version: existing databases just gain these tables on next start).
CREATE TABLE IF NOT EXISTS discounts (
    code         TEXT PRIMARY KEY,                       -- uppercase, what the customer says / types
    description  TEXT    NOT NULL,
    kind         TEXT    NOT NULL CHECK (kind IN ('percent','fixed')),
    value        REAL    NOT NULL CHECK (value > 0),     -- percent (0-100) or PHP amount
    min_spend    REAL    NOT NULL DEFAULT 0,             -- subtotal needed before the discount is valid
    valid_from   TEXT,                                   -- 'YYYY-MM-DD' (NULL = no start)
    valid_until  TEXT,                                   -- 'YYYY-MM-DD' inclusive (NULL = no end)
    max_uses     INTEGER,                                -- NULL = unlimited
    used_count   INTEGER NOT NULL DEFAULT 0,
    requires_id  INTEGER NOT NULL DEFAULT 0,             -- 1 = staff must see an ID (Senior / PWD)
    active       INTEGER NOT NULL DEFAULT 1,
    aliases      TEXT    NOT NULL DEFAULT ''             -- what customers say: 'senior,senior citizen'
);

CREATE TABLE IF NOT EXISTS session_discounts (          -- at most one discount per table session
    session_id INTEGER PRIMARY KEY REFERENCES table_sessions(session_id),
    code       TEXT NOT NULL REFERENCES discounts(code)
);

INSERT OR IGNORE INTO discounts (code, description, kind, value, min_spend, valid_from, valid_until, max_uses, requires_id, aliases) VALUES
 ('SENIOR20',  'Senior Citizen 20% discount',  'percent', 20,   0, NULL, NULL, NULL, 1, 'senior,senior citizen,senior discount,osca'),
 ('PWD20',     'PWD 20% discount',             'percent', 20,   0, NULL, NULL, NULL, 1, 'pwd,pwd discount,person with disability'),
 ('WELCOME10', 'Welcome promo: 10% off',       'percent', 10, 200, NULL, NULL, NULL, 0, 'welcome'),
 ('SAVE50',    'PHP 50 off orders of PHP 400+', 'fixed',   50, 400, NULL, NULL, 100, 0, 'save fifty'),
 ('EXPIRED5',  'Expired promo (edge-case test)', 'percent', 5,   0, NULL, '2020-01-01', NULL, 0, '');

INSERT OR IGNORE INTO menu_items (name, category, price, stock_qty, allergens, aliases) VALUES
 -- Burgers
 ('Big Mack Daddy',                   'Burgers',      195, 20, 'gluten,dairy,egg,sesame', 'big mac,big mack,mack daddy'),
 ('Quarter Pounder-ous Cheese',       'Burgers',      215, 15, 'gluten,dairy',            'quarter pounder,quarter pound,quarter pounder with cheese'),
 ('Cheesy Does It',                   'Burgers',       85, 25, 'gluten,dairy',            'cheeseburger,cheese burger,single cheeseburger'),
 ('Double Trouble Cheeseburger',      'Burgers',      130, 18, 'gluten,dairy',            'double cheeseburger,double cheese burger'),
 ('Lettuce Be Friends Veggie Burger', 'Burgers',      150, 12, 'gluten,soy',              'veggie burger,vegetarian burger,plant burger'),
 ('Fishy Business Sandwich',          'Burgers',      140, 10, 'gluten,fish,dairy,egg',   'filet o fish,fish sandwich,fish burger'),
 -- Chicken
 ('Cluck Norris Sandwich',            'Chicken',      145, 16, 'gluten,egg',              'chuck norris,chicken sandwich,mcchicken'),
 ('Nugget Life Chose Me (6pc)',       'Chicken',      125, 20, 'gluten',                  'nuggets,chicken nuggets,mcnuggets,nugget'),
 -- Rice & Pasta
 ('Cluck Yeah Fried Chicken',         'Rice & Pasta', 175, 14, 'gluten,egg,soy',          'fried chicken,chicken mcdo,chicken with rice'),
 ('Steak Me Seriously',               'Rice & Pasta', 110, 14, 'soy,gluten',              'burger steak,steak with rice'),
 ('Sweet Spaghet Alert',              'Rice & Pasta',  80, 18, 'gluten,dairy',            'spaghetti,spaghet,mcspaghetti'),
 -- Breakfast
 ('Egg-cellent Muffin',               'Breakfast',     99, 20, 'gluten,egg,dairy',        'egg mcmuffin,mcmuffin,muffin,sausage muffin'),
 ('Flap Jacked Hotcakes',             'Breakfast',     89, 15, 'gluten,egg,dairy',        'hotcakes,hot cakes,pancakes'),
 -- Sides
 ('Fry Hard',                         'Sides',         70, 40, '',                        'fries,french fries,fry'),
 ('Hash Tag Brown',                   'Sides',         45, 30, 'gluten',                  'hash brown,hash browns,hashbrown'),
 ('Corn-y Jokes',                     'Sides',         50, 20, '',                        'corn,corn cup'),
 -- Drinks
 ('Bubble Trouble Cola',              'Drinks',        55, 50, '',                        'coke,cola,soda,soft drink'),
 ('Lemon Lime Time',                  'Drinks',        55, 40, '',                        'sprite,lemon lime'),
 ('Brew-tiful Iced Tea',              'Drinks',        55, 40, '',                        'iced tea,ice tea,tea'),
 ('Wake-Up Call Coffee',              'Drinks',        60, 25, '',                        'coffee,black coffee'),
 ('Aqua Man Bottled Water',           'Drinks',        35, 50, '',                        'water,bottled water'),
 ('Shake Your Shake Maker',           'Drinks',        95, 12, 'dairy',                   'milkshake,milk shake,shake'),
 -- Desserts
 ('Swirl Me Maybe',                   'Desserts',      85, 15, 'dairy,gluten',            'mcflurry,flurry'),
 ('Sundae Funday',                    'Desserts',      60, 20, 'dairy',                   'sundae,ice cream sundae'),
 ('Cone Voyage',                      'Desserts',      30, 30, 'dairy,gluten',            'cone,ice cream cone,soft serve'),
 ('Apple of My Pie',                  'Desserts',      50, 18, 'gluten',                  'apple pie'),
 ('Mango Tango Pie',                  'Desserts',      50,  0, 'gluten',                  'mango pie');   -- out of stock on purpose (edge-case test)
