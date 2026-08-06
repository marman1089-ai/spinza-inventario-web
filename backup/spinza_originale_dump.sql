BEGIN TRANSACTION;
CREATE TABLE archived_stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            opened_at TEXT,
            closed_at TEXT,
            notes TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT 'system',
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE cash_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            flow_date TEXT NOT NULL,
            payment_method TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL DEFAULT 0,
            orders_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE cash_expense_category_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL DEFAULT 'ALL',
            pattern TEXT NOT NULL DEFAULT '',
            pattern_norm TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL DEFAULT 'system',
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE cash_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            flow_date TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            supplier TEXT NOT NULL DEFAULT '',
            payment_method TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE cash_payment_methods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_default INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT 'system',
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE closures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            closure_date TEXT NOT NULL,
            uploaded_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now')),
            filename TEXT,
            content_type TEXT,
            data BLOB
        );
CREATE TABLE invoice_import_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            supplier TEXT NOT NULL,
            doc_date TEXT NOT NULL,
            uploaded_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now')),
            filename TEXT,
            content_type TEXT,
            data BLOB
        );
CREATE TABLE invoice_import_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            raw_name TEXT NOT NULL,
            category TEXT NOT NULL,
            qty REAL NOT NULL,
            unit TEXT,
            product_id INTEGER,
            product_name TEXT
        );
CREATE TABLE invoice_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            invoice_doc_id INTEGER NOT NULL,
            supplier TEXT NOT NULL,
            doc_date TEXT NOT NULL,
            area TEXT NOT NULL,
            created_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE invoices_docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            supplier TEXT NOT NULL,
            doc_date TEXT NOT NULL,
            uploaded_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now')),
            filename TEXT,
            content_type TEXT,
            data BLOB
        );
CREATE TABLE logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        username TEXT NOT NULL,
        action TEXT NOT NULL,
        category TEXT NOT NULL,
        name TEXT NOT NULL,
        delta REAL NOT NULL
    , store TEXT NOT NULL DEFAULT 'spinza');
INSERT INTO "logs" VALUES(1,'2025-11-23 20:29:41','marco','AGGIUNTO','CUCINA','mozzarella',0.0,'spinza');
INSERT INTO "logs" VALUES(2,'2025-11-23 21:09:13','marco','ELIMINATO','CUCINA','mozzarella',0.0,'spinza');
INSERT INTO "logs" VALUES(3,'2025-11-24 00:23:33','marco','AGGIUNTO','VINO','ancestrale',5.0,'spinza');
INSERT INTO "logs" VALUES(4,'2025-11-24 00:24:03','marco','AGGIUNTO','VINO','libello',3.0,'spinza');
INSERT INTO "logs" VALUES(5,'2025-11-24 00:24:26','marco','AGGIORNATO','VINO','ancestrale',4.0,'spinza');
INSERT INTO "logs" VALUES(6,'2025-11-24 00:25:05','marco','AGGIUNTO','CUCINA','farina00',3.0,'spinza');
INSERT INTO "logs" VALUES(7,'2025-11-24 00:28:09','marco','AGGIUNTO','GIULIA','giulie',1.0,'spinza');
INSERT INTO "logs" VALUES(8,'2025-11-24 11:48:32','marco','ELIMINATO','GIULIA','giulie',1.0,'spinza');
INSERT INTO "logs" VALUES(9,'2025-11-24 13:49:13','marco','AGGIORNATO','CUCINA','farina00',3.0,'spinza');
INSERT INTO "logs" VALUES(10,'2025-11-24 13:50:24','marco','AGGIUNTO','VINO','gazza ladra',4.0,'spinza');
INSERT INTO "logs" VALUES(11,'2025-11-24 13:50:38','marco','AGGIORNATO','VINO','gazza ladra',4.0,'spinza');
INSERT INTO "logs" VALUES(12,'2025-11-24 19:21:35','marco','AGGIORNATO','VINO','ancestrale',1.0,'spinza');
INSERT INTO "logs" VALUES(13,'2025-11-24 19:21:52','marco','AGGIORNATO','VINO','gazza ladra',4.0,'spinza');
INSERT INTO "logs" VALUES(14,'2025-11-24 19:22:18','marco','AGGIORNATO','VINO','libello',7.0,'spinza');
INSERT INTO "logs" VALUES(15,'2025-11-24 19:22:43','marco','AGGIUNTO','VINO','ho rosa!',7.0,'spinza');
INSERT INTO "logs" VALUES(16,'2025-11-24 19:22:51','marco','AGGIORNATO','VINO','gazza ladra',6.0,'spinza');
INSERT INTO "logs" VALUES(17,'2025-11-24 19:23:03','marco','AGGIORNATO','VINO','gazza ladra',4.0,'spinza');
INSERT INTO "logs" VALUES(18,'2025-11-24 19:23:21','marco','AGGIUNTO','VINO','inganno felice',3.0,'spinza');
INSERT INTO "logs" VALUES(19,'2025-11-24 19:23:39','marco','AGGIUNTO','VINO','la papessa',8.0,'spinza');
INSERT INTO "logs" VALUES(20,'2025-11-24 19:23:59','marco','AGGIUNTO','VINO','vermentino',10.0,'spinza');
INSERT INTO "logs" VALUES(21,'2025-11-24 19:24:18','marco','AGGIORNATO','VINO','la papessa',10.0,'spinza');
INSERT INTO "logs" VALUES(22,'2025-11-24 19:35:46','marco','ELIMINATO','CUCINA','farina00',3.0,'spinza');
INSERT INTO "logs" VALUES(23,'2025-11-24 19:38:32','marco','AGGIUNTO','VINO','pandora',0.0,'spinza');
INSERT INTO "logs" VALUES(24,'2025-11-24 19:40:16','marco','AGGIUNTO','KOMBUCHA','mockito',0.0,'spinza');
INSERT INTO "logs" VALUES(25,'2025-11-24 19:40:31','marco','AGGIUNTO','KOMBUCHA','ginger bomb',0.0,'spinza');
INSERT INTO "logs" VALUES(26,'2025-11-24 19:41:00','marco','AGGIUNTO','KOMBUCHA','bloomy orange',0.0,'spinza');
INSERT INTO "logs" VALUES(27,'2025-11-24 19:43:06','marco','AGGIUNTO','LIMONATA','limonata sanpellegrino',24.0,'spinza');
INSERT INTO "logs" VALUES(28,'2025-11-24 19:43:59','marco','AGGIUNTO','COCA COLA','normale vetro',72.0,'spinza');
INSERT INTO "logs" VALUES(29,'2025-11-24 19:44:29','marco','AGGIUNTO','COCA COLA','zero vetro',96.0,'spinza');
INSERT INTO "logs" VALUES(30,'2025-11-24 19:45:02','marco','AGGIUNTO','COCA COLA','normale lattina',48.0,'spinza');
INSERT INTO "logs" VALUES(31,'2025-11-24 19:45:32','marco','AGGIUNTO','COCA COLA','zero lattina',45.0,'spinza');
INSERT INTO "logs" VALUES(32,'2025-11-24 19:47:05','marco','AGGIUNTO','ACQUA','naturale',80.0,'spinza');
INSERT INTO "logs" VALUES(33,'2025-11-24 19:47:26','marco','AGGIUNTO','ACQUA','frizzante',40.0,'spinza');
INSERT INTO "logs" VALUES(34,'2025-11-24 19:47:38','marco','AGGIORNATO','ACQUA','frizzante',40.0,'spinza');
INSERT INTO "logs" VALUES(35,'2025-11-24 19:47:49','marco','AGGIORNATO','ACQUA','naturale',80.0,'spinza');
INSERT INTO "logs" VALUES(36,'2025-11-24 19:47:57','marco','AGGIORNATO','ACQUA','frizzante',40.0,'spinza');
INSERT INTO "logs" VALUES(37,'2025-11-24 19:50:06','marco','AGGIUNTO','BIRRA','session ipa',14.0,'spinza');
INSERT INTO "logs" VALUES(38,'2025-11-24 20:49:13','marco','AGGIUNTO','BIRRA','german ale',40.0,'spinza');
INSERT INTO "logs" VALUES(39,'2025-11-24 20:49:54','marco','AGGIUNTO','BIRRA','american ipa',14.0,'spinza');
INSERT INTO "logs" VALUES(40,'2025-11-24 20:51:16','marco','AGGIUNTO','FARINA','petra',25.0,'spinza');
INSERT INTO "logs" VALUES(41,'2025-11-24 20:53:34','marco','AGGIUNTO','FARINA','riso',25.0,'spinza');
INSERT INTO "logs" VALUES(42,'2025-11-24 20:55:08','marco','AGGIUNTO','FARINA','semola',2.0,'spinza');
INSERT INTO "logs" VALUES(43,'2025-11-24 20:55:12','marco','ELIMINATO','FARINA','petra',25.0,'spinza');
INSERT INTO "logs" VALUES(44,'2025-11-24 20:56:14','marco','AGGIUNTO','FRIGO','mozzarella',2.0,'spinza');
INSERT INTO "logs" VALUES(45,'2025-11-24 20:56:22','marco','AGGIORNATO','FRIGO','mozzarella',2.0,'spinza');
INSERT INTO "logs" VALUES(46,'2025-11-24 20:56:34','marco','AGGIORNATO','FARINA','semola',1.0,'spinza');
INSERT INTO "logs" VALUES(47,'2025-11-24 20:56:52','marco','AGGIORNATO','FARINA','riso',2.0,'spinza');
INSERT INTO "logs" VALUES(48,'2025-11-24 20:57:45','marco','AGGIUNTO','FRIGO','bufala',7.0,'spinza');
INSERT INTO "logs" VALUES(49,'2025-11-24 20:58:30','marco','AGGIUNTO','FRIGO','salamino',8.0,'spinza');
INSERT INTO "logs" VALUES(50,'2025-11-24 20:58:46','marco','AGGIUNTO','FRIGO','lardo colonnata',1.0,'spinza');
INSERT INTO "logs" VALUES(51,'2025-11-24 20:59:25','marco','AGGIUNTO','FRIGO','prosciutto crudo',1.0,'spinza');
INSERT INTO "logs" VALUES(52,'2025-11-24 20:59:50','marco','AGGIUNTO','FRIGO','nduja',1.0,'spinza');
INSERT INTO "logs" VALUES(53,'2025-11-24 21:00:00','marco','AGGIUNTO','FRIGO','porcchetta',1.0,'spinza');
INSERT INTO "logs" VALUES(54,'2025-11-24 21:01:34','marco','AGGIUNTO','FRIZER','crema al formaggio',3.0,'spinza');
INSERT INTO "logs" VALUES(55,'2025-11-24 21:01:47','marco','AGGIUNTO','FRIZER','crema broccoli',1.0,'spinza');
INSERT INTO "logs" VALUES(56,'2025-11-24 21:02:05','marco','AGGIUNTO','FRIZER','crema zucca',0.0,'spinza');
INSERT INTO "logs" VALUES(57,'2025-11-24 21:03:17','marco','AGGIUNTO','FRIZER','biscotti tiramisu',27.0,'spinza');
INSERT INTO "logs" VALUES(58,'2025-11-24 21:04:12','marco','AGGIUNTO','FRIZER','porcchini',0.0,'spinza');
INSERT INTO "logs" VALUES(59,'2025-11-24 21:05:28','marco','AGGIUNTO','FRIZER','basi integrali',22.0,'spinza');
INSERT INTO "logs" VALUES(60,'2025-11-24 21:06:49','marco','AGGIUNTO','FRIZER','basi staff',9.0,'spinza');
INSERT INTO "logs" VALUES(61,'2025-11-24 21:06:57','marco','AGGIORNATO','FRIZER','basi staff',9.0,'spinza');
INSERT INTO "logs" VALUES(62,'2025-11-26 18:52:39','marco','AGGIORNATO','COCA COLA','normale lattina',72.0,'spinza');
INSERT INTO "logs" VALUES(63,'2025-11-26 18:53:13','marco','AGGIORNATO','COCA COLA','zero lattina',69.0,'spinza');
INSERT INTO "logs" VALUES(64,'2025-11-26 18:57:31','marco','AGGIORNATO','KOMBUCHA','mockito',12.0,'spinza');
INSERT INTO "logs" VALUES(65,'2025-11-26 19:08:40','marco','AGGIORNATO','LIMONATA','limonata sanpellegrino',0.0,'spinza');
INSERT INTO "logs" VALUES(66,'2025-11-26 19:08:52','marco','AGGIORNATO','FRIGO','mozzarella',1.0,'spinza');
INSERT INTO "logs" VALUES(67,'2025-11-26 19:09:04','marco','AGGIORNATO','FRIGO','bufala',4.0,'spinza');
INSERT INTO "logs" VALUES(68,'2025-11-26 19:53:30','marco','AGGIORNATO','BIRRA','session ipa',0.0,'spinza');
INSERT INTO "logs" VALUES(69,'2025-11-26 19:53:37','marco','AGGIORNATO','BIRRA','american ipa',4.0,'spinza');
INSERT INTO "logs" VALUES(70,'2025-11-26 19:56:48','marco','AGGIORNATO','BIRRA','german ale',34.0,'spinza');
INSERT INTO "logs" VALUES(71,'2025-11-26 20:05:35','marco','AGGIORNATO','COCA COLA','zero lattina',66.0,'spinza');
INSERT INTO "logs" VALUES(72,'2025-11-26 20:05:47','marco','AGGIORNATO','ACQUA','frizzante',20.0,'spinza');
INSERT INTO "logs" VALUES(73,'2025-11-26 20:24:00','marco','AGGIORNATO','VINO','libello',6.0,'spinza');
INSERT INTO "logs" VALUES(74,'2025-11-26 21:32:54','marco','AGGIORNATO','FRIGO','bufala',0.0,'spinza');
INSERT INTO "logs" VALUES(75,'2025-11-26 22:07:34','marco','AGGIORNATO','VINO','libello',5.0,'spinza');
INSERT INTO "logs" VALUES(76,'2025-11-27 18:18:55','marco','ELIMINATO','KOMBUCHA','bloomy orange',0.0,'spinza');
INSERT INTO "logs" VALUES(77,'2025-11-28 17:53:03','marco','AGGIUNTO','FRIGO','salsiccia',1.0,'spinza');
INSERT INTO "logs" VALUES(78,'2025-11-28 17:53:17','marco','AGGIUNTO','FRIGO','pomiodori secchi',1.0,'spinza');
INSERT INTO "logs" VALUES(79,'2025-11-28 17:54:01','marco','AGGIUNTO','FRIGO','fior di caprino',7.0,'spinza');
INSERT INTO "logs" VALUES(80,'2025-11-28 17:54:28','marco','AGGIUNTO','FRIGO','ricotta',5.0,'spinza');
INSERT INTO "logs" VALUES(81,'2025-11-28 17:54:48','marco','AGGIORNATO','FRIGO','fior di caprino',7.0,'spinza');
INSERT INTO "logs" VALUES(82,'2025-11-28 17:55:29','marco','AGGIORNATO','FRIGO','mozzarella',7.0,'spinza');
INSERT INTO "logs" VALUES(83,'2025-11-28 17:55:52','marco','AGGIUNTO','FRIGO','parmigiano regiano',1.0,'spinza');
INSERT INTO "logs" VALUES(84,'2025-11-28 17:56:05','marco','AGGIUNTO','FRIGO','pecorino romano',1.0,'spinza');
INSERT INTO "logs" VALUES(85,'2025-11-28 17:57:34','marco','AGGIUNTO','CUCINA','salsa pomodoro',0.0,'spinza');
INSERT INTO "logs" VALUES(86,'2025-11-28 17:58:04','marco','AGGIORNATO','FARINA','riso',20.0,'spinza');
INSERT INTO "logs" VALUES(87,'2025-11-28 17:59:22','marco','AGGIORNATO','CUCINA','salsa pomodoro',7.0,'spinza');
INSERT INTO "logs" VALUES(88,'2025-12-27 15:46:15','marco','AGGIORNATO','ACQUA','frizzante',20.0,'spinza');
INSERT INTO "logs" VALUES(89,'2025-12-27 15:46:26','marco','AGGIORNATO','ACQUA','frizzante',20.0,'spinza');
INSERT INTO "logs" VALUES(90,'2026-01-05 11:13:22','marco','AGGIORNATO','ACQUA','naturale',60.0,'spinza');
CREATE TABLE order_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            qty REAL NOT NULL,
            received_qty REAL NOT NULL DEFAULT 0,
            is_missing INTEGER NOT NULL DEFAULT 0
        , area TEXT, unit TEXT);
CREATE TABLE order_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            qty_to_order REAL NOT NULL DEFAULT 1,
            added_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            supplier TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'in_corso',
            created_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now')),
            closed_at TEXT
        , kind TEXT DEFAULT 'ordine', from_store TEXT, to_store TEXT, transfer_id INTEGER);
CREATE TABLE products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        name TEXT NOT NULL,
        qty REAL NOT NULL DEFAULT 0,
        min_qty REAL NOT NULL DEFAULT 0, store TEXT NOT NULL DEFAULT 'spinza', area TEXT NOT NULL DEFAULT 'prodotti', unit TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT 'MAGAZZINO', missing_order_date TEXT, missing_delivery_date TEXT, missing_qty REAL NOT NULL DEFAULT 0, category_color TEXT NOT NULL DEFAULT '#64748b',
        UNIQUE(category, name)
    );
INSERT INTO "products" VALUES(1,'VINO','ancestrale',1.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(2,'VINO','libello',5.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(3,'VINO','gazza ladra',4.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(4,'VINO','ho rosa!',7.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(5,'VINO','inganno felice',3.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(6,'VINO','la papessa',10.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(7,'VINO','vermentino',10.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(8,'VINO','pandora',0.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(9,'KOMBUCHA','mockito',12.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(10,'KOMBUCHA','ginger bomb',0.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(11,'LIMONATA','limonata sanpellegrino',0.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(12,'COCA COLA','normale vetro',72.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(13,'COCA COLA','zero vetro',96.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(14,'COCA COLA','normale lattina',72.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(15,'COCA COLA','zero lattina',66.0,6.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(16,'ACQUA','naturale',60.0,21.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(17,'ACQUA','frizzante',20.0,23.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(18,'BIRRA','session ipa',0.0,10.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(19,'BIRRA','german ale',34.0,10.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(20,'BIRRA','american ipa',4.0,10.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(21,'FARINA','riso',20.0,5.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(22,'FARINA','semola',1.0,2.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(23,'FRIGO','mozzarella',7.0,3.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(24,'FRIGO','bufala',0.0,8.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(25,'FRIGO','salamino',8.0,4.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(26,'FRIGO','lardo colonnata',1.0,1.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(27,'FRIGO','prosciutto crudo',1.0,1.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(28,'FRIGO','nduja',1.0,2.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(29,'FRIGO','porcchetta',1.0,1.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(30,'FRIZER','crema al formaggio',3.0,2.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(31,'FRIZER','crema broccoli',1.0,2.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(32,'FRIZER','crema zucca',0.0,2.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(33,'FRIZER','biscotti tiramisu',27.0,12.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(34,'FRIZER','porcchini',0.0,3.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(35,'FRIZER','basi integrali',22.0,10.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(36,'FRIZER','basi staff',9.0,0.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(37,'FRIGO','salsiccia',1.0,1.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(38,'FRIGO','pomiodori secchi',1.0,1.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(39,'FRIGO','fior di caprino',7.0,3.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(40,'FRIGO','ricotta',5.0,2.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(41,'FRIGO','parmigiano regiano',1.0,1.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(42,'FRIGO','pecorino romano',1.0,1.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
INSERT INTO "products" VALUES(43,'CUCINA','salsa pomodoro',7.0,3.0,'spinza','prodotti','','MAGAZZINO',NULL,NULL,0.0,'#64748b');
CREATE TABLE sales_report_group_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            name TEXT NOT NULL,
            name_norm TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_by TEXT NOT NULL DEFAULT 'system',
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE sales_report_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id INTEGER NOT NULL,
            parent_id INTEGER,
            name TEXT NOT NULL,
            base_name TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL DEFAULT 0,
            quantity REAL NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE sales_report_name_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            source_name_norm TEXT NOT NULL,
            source_name TEXT NOT NULL DEFAULT '',
            target_group_name TEXT NOT NULL DEFAULT '',
            target_name TEXT NOT NULL DEFAULT '',
            is_deleted INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT 'system',
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE sales_report_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            month_key TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE secondary_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT NOT NULL,
            expense_date TEXT NOT NULL,
            uploaded_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now')),
            filename TEXT,
            content_type TEXT,
            data BLOB
        );
CREATE TABLE transfer_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_id INTEGER NOT NULL,
            from_store TEXT NOT NULL,
            to_store TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            area TEXT NOT NULL,
            qty REAL NOT NULL,
            unit TEXT NOT NULL DEFAULT ''
        );
CREATE TABLE transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_store TEXT NOT NULL,
            to_store TEXT NOT NULL,
            created_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            message TEXT NOT NULL,
            created_by TEXT NOT NULL,
            ts TEXT DEFAULT (datetime('now'))
        );
CREATE TABLE users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        role TEXT NOT NULL DEFAULT 'staff',
        pw_salt TEXT,
        pw_hash TEXT,
        legacy_sha256 TEXT
    , store TEXT NOT NULL DEFAULT 'spinza', last_seen TEXT);
INSERT INTO "users" VALUES(1,'marco','staff',NULL,NULL,'e9b88633658e5a6f9105e593605f2af62f37afb53d1bc94362e253b8f813188c','spinza',NULL);
INSERT INTO "users" VALUES(2,'marco06','admin','JNh-iQPBaRLYjFZmdM9avg==','2mEAJYhaff1txTXbsMI07FZ5l6srL4_hpUuB4AQcxOI=',NULL,'spinza',NULL);
INSERT INTO "users" VALUES(3,'admin','admin','laUft7zF_JaKZB5Km2tsMA==','4OrQowqfQ8CfS2chs4Uww3xvZN2Hi6_AYG56dIuMQOs=',NULL,'spinza','2026-04-10 17:08:11');
CREATE UNIQUE INDEX ux_users_store_username ON users(store, username);
CREATE UNIQUE INDEX ux_users_admin_username
            ON users(username)
            WHERE role = 'admin'
            ;
CREATE UNIQUE INDEX ux_products_store_cat_name_loc
            ON products(store, area, category, name, location)
            ;
CREATE UNIQUE INDEX ux_invoice_imports_store_doc
        ON invoice_imports(store, invoice_doc_id)
        ;
CREATE UNIQUE INDEX ux_sales_report_period_store_month ON sales_report_periods(store, month_key);
CREATE UNIQUE INDEX ux_sales_report_group_models_store_name ON sales_report_group_models(store, name_norm);
CREATE UNIQUE INDEX ux_sales_report_name_rules_store_source ON sales_report_name_rules(store, source_name_norm);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('products',43);
INSERT INTO "sqlite_sequence" VALUES('users',3);
INSERT INTO "sqlite_sequence" VALUES('logs',90);
INSERT INTO "sqlite_sequence" VALUES('archived_stores',1);
INSERT INTO "sqlite_sequence" VALUES('cash_entries',1);
INSERT INTO "sqlite_sequence" VALUES('cash_expenses',1);
COMMIT;
