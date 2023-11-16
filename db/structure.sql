
CREATE TABLE entry (
  id int(11) NOT NULL AUTO_INCREMENT,
  room_id int(11) NOT NULL,
  date date NOT NULL,
  capacity int(11) NOT NULL,
  pos int(11) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY room_id_date (room_id,date),
  KEY date (date),
  CONSTRAINT room_id FOREIGN KEY (room_id) REFERENCES room (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE room (
  id int(11) NOT NULL AUTO_INCREMENT,
  user_id int(11) NOT NULL,
  ext_id int(11) NOT NULL,
  type varchar(100) DEFAULT NULL,
  description varchar(100) DEFAULT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_ext_id (ext_id),
  UNIQUE KEY uniq_type_desription (type,description),
  KEY user_id (user_id),
  CONSTRAINT user_id FOREIGN KEY (user_id) REFERENCES user (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE user (
  id int(11) NOT NULL AUTO_INCREMENT,
  email varchar(100) DEFAULT NULL,
  password varchar(100) DEFAULT NULL,
  rmc_value varchar(255) DEFAULT NULL,
  rmc_expiry datetime DEFAULT NULL,
  firstname varchar(100) NOT NULL,
  lastname varchar(100) NOT NULL,
  goal_date date NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
