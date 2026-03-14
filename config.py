import os


MYSQL_HOST = os.environ.get("MYSQLHOST")
MYSQL_USER = os.environ.get("MYSQLUSER")
MYSQL_PASSWORD = os.environ.get("MYSQLPASSWORD")
MYSQL_DB = os.environ.get("MYSQLDATABASE")
MYSQL_PORT = int(os.environ.get("MYSQLPORT", 3306))# your database name
MAIL_USER = "ghana19183@gmail.com"
MAIL_PASS = "jmwe kafu lmdp cmpr"   # Use Gmail App Password (NOT your Gmail login)

