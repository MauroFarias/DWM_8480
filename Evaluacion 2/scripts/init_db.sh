#!/bin/sh
# Una base y un usuario por servicio: cada servicio solo ve sus propios datos.
set -e
for s in auth clients inventory orders payments notifications; do
  eval "pw=\$DB_PASS_$(echo $s | tr a-z A-Z)"
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" <<SQL
CREATE USER $s WITH PASSWORD '$pw';
CREATE DATABASE $s OWNER $s;
SQL
done
