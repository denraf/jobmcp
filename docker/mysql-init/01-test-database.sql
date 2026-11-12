-- Runs once, when the MySQL volume is first created. The tests drop and recreate every table in this database.
CREATE DATABASE IF NOT EXISTS livejobs_test CHARACTER SET utf8mb4;
GRANT ALL PRIVILEGES ON livejobs_test.* TO 'livejobs'@'%';
