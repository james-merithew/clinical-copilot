<?php
//  OpenEMR MySQL Config — BAKED into the Railway image (demo hardening).
//
//  Why this file exists: on Railway the container filesystem (including
//  sites/default) resets on every restart, and the stock image ships
//  $config = 0 — so the entrypoint re-ran the auto-installer and WIPED the
//  database on each boot. Baking a configured sqlconf.php ($config = 1) makes
//  the entrypoint skip auto_configure entirely and start Apache against the
//  already-installed, persistent MariaDB.
//
//  Credentials are read from the service's environment (Railway private vars)
//  at include time — nothing secret is baked into the image or the repo.

$host  = getenv('MYSQL_HOST') ?: 'localhost';
$port  = getenv('MYSQL_PORT') ?: '3306';
$login = getenv('MYSQL_USER') ?: 'openemr';
$pass  = getenv('MYSQL_PASS') ?: 'openemr';
$dbase = getenv('MYSQL_DATABASE') ?: 'openemr';

$sqlconf = array();
global $sqlconf;
$sqlconf["host"]  = $host;
$sqlconf["port"]  = $port;
$sqlconf["login"] = $login;
$sqlconf["pass"]  = $pass;
$sqlconf["dbase"] = $dbase;

//////////////////////////
//////////////////////////
//////////////////////////
//////DO NOT TOUCH THIS///
$config = 1; /////////////
//////////////////////////
//////////////////////////
//////////////////////////
