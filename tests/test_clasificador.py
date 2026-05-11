from app.clasificador import clasificar_descripcion


def test_kennel_a_casa_jaula():
    assert clasificar_descripcion("Plastic pet kennel (Plastic dog house), Small") == "casa-jaula"


def test_food_feeder_a_alimentadores():
    assert clasificar_descripcion("Plastic pet food feeder, with two steel bowls") == "alimentadores"


def test_water_dispenser_a_bebederos():
    assert clasificar_descripcion("Water dispenser quotation") == "bebederos"


def test_pet_gate_a_rejas():
    assert clasificar_descripcion("Plastic pet gate small") == "rejas"


def test_pet_bed_a_camas():
    assert clasificar_descripcion("Window cat bed for small pets") == "camas"


def test_pet_carrier_a_transporte():
    assert clasificar_descripcion("Pet carrier bag with mesh") == "transporte"


def test_leash_a_correas():
    assert clasificar_descripcion("Pet collar leash harness catalogue") == "correas"


def test_descripcion_vacia_es_none():
    assert clasificar_descripcion("") is None
    assert clasificar_descripcion(None) is None


def test_sin_match_es_none():
    assert clasificar_descripcion("Sample text without any pet keyword") is None


def test_pajaros_antes_que_alimentadores():
    # 'bird feeder' debe quedar en 'pajaros' (mas especifico) por orden de reglas
    assert clasificar_descripcion("hummingbird feeder USD") == "pajaros"


def test_case_insensitive():
    assert clasificar_descripcion("PLASTIC PET KENNEL") == "casa-jaula"


def test_food_bowl_a_alimentadores():
    assert clasificar_descripcion("Pet dog food bowl") == "alimentadores"


def test_feeding_mat_a_alimentadores():
    assert clasificar_descripcion("Thickened Pet Feeding Mat, Nonslip") == "alimentadores"


def test_water_bottle_a_bebederos():
    assert clasificar_descripcion("Portable Dog Water Bottle for Outdoor Use") == "bebederos"


def test_comb_a_higiene():
    assert clasificar_descripcion("Pet Dematting Comb for Cats and Dogs") == "higiene"


def test_paw_cleaner_a_higiene():
    assert clasificar_descripcion("Pet Paw Washing Cup, Dog Paw Cleaner") == "higiene"


def test_cat_tunnel_a_juguetes():
    assert clasificar_descripcion("Foldable Cat Tunnel Toy 2kg polyester") == "juguetes"


def test_shoulder_bag_a_transporte():
    assert clasificar_descripcion("New Portable Pet Shoulder Bag for Cats") == "transporte"


def test_pet_stroller_a_transporte():
    assert clasificar_descripcion("Small pet stroller for dogs, cats") == "transporte"


def test_seat_cover_a_transporte():
    assert clasificar_descripcion("Single Rear Seat Car Pet Seat Cover") == "transporte"


def test_air_box_a_transporte():
    assert clasificar_descripcion("Pet air box Large") == "transporte"
