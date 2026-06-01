from __future__ import annotations

import unittest

from models import NormalizedProduct
from packaging import interpret_packaging


class PackagingTests(unittest.TestCase):
    def make_product(self, **overrides: str) -> NormalizedProduct:
        payload = {
            "product_url": "https://supplier.example/test",
            "canonical_url": "https://supplier.example/test",
            "category_path": "Test",
            "product_name": "Test Product",
            "item_name": "Test Product",
            "sku": "TEST",
            "price": "10.00",
            "currency": "CHF",
            "price_per": "vessel",
            "order_by": "vessel",
            "min_order_count": "1",
            "status": "ACTIVE",
            "vessel_size": "1",
            "vessel_unit": "quantity",
            "vessel_type": "",
            "bundle_size": "",
            "bundle_type": "",
            "raw_bundle_text": "",
            "raw_detail_price_unit_text": "",
            "raw_spec_piece_text": "",
            "raw_fill_text": "",
            "specs": {},
        }
        payload.update(overrides)
        return NormalizedProduct(**payload)

    def test_flattens_piece_pack_into_vessel(self) -> None:
        product = self.make_product(
            sku="YST013",
            item_name="Trinkhalm ÖKO-LINE, Ø 6 x 195 mm, gelb",
            price="4.25",
            vessel_type="straw",
            raw_detail_price_unit_text="100 Stück / Pack",
            raw_spec_piece_text="100 Stk. / Pack",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "flatten_piece_pack")
        self.assertEqual(decision.vessel_size, "100")
        self.assertEqual(decision.vessel_unit, "quantity")
        self.assertEqual(decision.bundle_size, "")
        self.assertEqual(decision.price, "4.25")

    def test_preserves_bottle_unit_and_divides_price(self) -> None:
        product = self.make_product(
            sku="4868201",
            item_name="Händedesinfektionsmittel Germex mano plus, Spender-Flasche - Standardmenge",
            price="96.25",
            raw_detail_price_unit_text="6 Flaschen / Karton",
            raw_spec_piece_text="6 Flaschen",
            raw_fill_text="500 ml / Flasche",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "preserve_inner_unit_divide_price")
        self.assertEqual(decision.vessel_size, "500")
        self.assertEqual(decision.vessel_unit, "ml")
        self.assertEqual(decision.vessel_type, "bottle")
        self.assertEqual(decision.bundle_size, "6")
        self.assertEqual(decision.bundle_type, "Karton")
        self.assertEqual(decision.price, "16.04")

    def test_preserves_roll_as_inner_unit(self) -> None:
        product = self.make_product(
            sku="116233",
            item_name="Frischhaltefolie 30 cm x 300 m",
            price="41.40",
            raw_detail_price_unit_text="4 Rollen / Karton",
            raw_spec_piece_text="4 Rollen / Karton",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "preserve_inner_unit_divide_price")
        self.assertEqual(decision.vessel_size, "1")
        self.assertEqual(decision.vessel_unit, "quantity")
        self.assertEqual(decision.vessel_type, "roll")
        self.assertEqual(decision.bundle_size, "4")
        self.assertEqual(decision.price, "10.35")

    def test_preserves_roll_when_bundle_is_expressed_as_pieces(self) -> None:
        product = self.make_product(
            sku="926768",
            item_name="Kassen-Rollen weiss, Breite 76 mm x 70 m, Kern Ø 12 mm",
            price="84.50",
            raw_detail_price_unit_text="50 Stück / Pack",
            raw_spec_piece_text="50 Stück / Pack",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "piece_count_in_named_unit")
        self.assertEqual(decision.vessel_size, "50")
        self.assertEqual(decision.vessel_unit, "quantity")
        self.assertEqual(decision.vessel_type, "roll")
        self.assertEqual(decision.bundle_size, "")
        self.assertEqual(decision.bundle_type, "")
        self.assertEqual(decision.price, "84.50")

    def test_uses_inner_count_for_nested_roll_case(self) -> None:
        product = self.make_product(
            sku="20345W",
            item_name="Abfallsack Compobag ÖKO-LINE, 110 Liter, 0.035 mm, grün - Standardmenge",
            price="171.90",
            raw_detail_price_unit_text="10 Rollen / Karton",
            raw_spec_piece_text="10 Rollen à 10 Stk. / Karton",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "preserve_inner_unit_divide_price")
        self.assertEqual(decision.vessel_size, "10")
        self.assertEqual(decision.vessel_unit, "quantity")
        self.assertEqual(decision.vessel_type, "roll")
        self.assertEqual(decision.bundle_size, "10")
        self.assertEqual(decision.bundle_type, "Karton")
        self.assertEqual(decision.price, "17.19")

    def test_uses_inner_count_for_nested_pack_case(self) -> None:
        product = self.make_product(
            sku="552719",
            item_name="Kopierpapier A4 120g/m² FSC, 210 x 297 mm",
            price="211.90",
            raw_detail_price_unit_text="5 Pack / Karton",
            raw_spec_piece_text="5 Pack x 250 Blatt",
            vessel_size="120",
            vessel_unit="g",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "preserve_inner_unit_divide_price")
        self.assertEqual(decision.vessel_size, "250")
        self.assertEqual(decision.vessel_unit, "quantity")
        self.assertEqual(decision.vessel_type, "pack")
        self.assertEqual(decision.bundle_size, "5")
        self.assertEqual(decision.bundle_type, "Karton")
        self.assertEqual(decision.price, "42.38")

    def test_marks_conflicting_detail_bundle_as_contaminated(self) -> None:
        product = self.make_product(
            sku="V35873",
            item_name="VIKAN Handbürste M, medium, 165 mm, Borstenlänge 26 mm, blau",
            price="8.90",
            raw_detail_price_unit_text="12 Flaschen / Karton",
            raw_spec_piece_text="1 Stk.",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "suspected_contaminated_bundle")

    def test_prefers_piece_spec_over_pack_alias_in_detail_block(self) -> None:
        product = self.make_product(
            sku="444325",
            item_name='Cocktailspiesse "Herz" 20 cm',
            price="3.95",
            raw_detail_price_unit_text="100 Pack / Karton",
            description="Grösse: 20 cm Stückzahl: 100 Stk. / Karton",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "flatten_piece_pack")
        self.assertEqual(decision.vessel_size, "100")
        self.assertEqual(decision.vessel_unit, "quantity")
        self.assertEqual(decision.bundle_size, "")
        self.assertEqual(decision.price, "3.95")

    def test_preserve_inner_unit_division_is_idempotent(self) -> None:
        product = self.make_product(
            sku="4868201",
            item_name="Händedesinfektionsmittel Germex mano plus, Spender-Flasche - Standardmenge",
            price="16.04",
            vessel_size="500",
            vessel_unit="ml",
            vessel_type="bottle",
            bundle_size="6",
            bundle_type="Karton",
            raw_detail_price_unit_text="6 Flaschen / Karton",
            raw_spec_piece_text="6 Flaschen",
            raw_fill_text="500 ml / Flasche",
            packaging_mode="preserve_inner_unit_divide_price",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "preserve_inner_unit_divide_price")
        self.assertEqual(decision.price, "16.04")

    def test_preserves_single_sack_without_bundle(self) -> None:
        product = self.make_product(
            sku="14493W",
            item_name="OMO Professional Advance Vollwaschmittel, parfümiert, pulverförmig, 14,25 kg",
            price="70.15",
            vessel_size="14.25",
            vessel_unit="kg",
            bundle_size="14.25",
            bundle_type="Sack",
            raw_detail_price_unit_text="14.25 kg / Sack",
            raw_spec_piece_text="1 Sack",
            raw_fill_text="14,25 kg / Sack",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "preserve_single_named_unit")
        self.assertEqual(decision.vessel_size, "14.25")
        self.assertEqual(decision.vessel_unit, "kg")
        self.assertEqual(decision.bundle_size, "")
        self.assertEqual(decision.bundle_type, "")

    def test_preserves_single_bottle_without_bundle(self) -> None:
        product = self.make_product(
            sku="5014301X",
            item_name="Flächendesinfektionsmittel Germex Spray, Flasche",
            price="18.70",
            vessel_size="500",
            vessel_unit="ml",
            vessel_type="bottle",
            bundle_size="0.5",
            bundle_type="Flasche",
            raw_detail_price_unit_text="0.5 Liter / Flasche",
            raw_spec_piece_text="1 Einzelflasche",
            raw_fill_text="500 ml / Flasche",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "preserve_single_named_unit")
        self.assertEqual(decision.vessel_size, "500")
        self.assertEqual(decision.vessel_unit, "ml")
        self.assertEqual(decision.bundle_size, "")
        self.assertEqual(decision.bundle_type, "")

    def test_uses_roll_piece_count_for_small_trash_bag_roll(self) -> None:
        product = self.make_product(
            sku="20345WX",
            item_name="Abfallsack Compobag ÖKO-LINE, 110 Liter, 0.035 mm, grün, kl.M.",
            price="18.90",
            vessel_size="110",
            vessel_unit="l",
            raw_detail_price_unit_text="10 Stk. / Rolle",
            raw_spec_piece_text="10 Stk. / Rolle",
            raw_fill_text="110 Liter",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "piece_count_in_named_unit")
        self.assertEqual(decision.vessel_size, "10")
        self.assertEqual(decision.vessel_unit, "quantity")
        self.assertEqual(decision.vessel_type, "roll")
        self.assertEqual(decision.bundle_size, "")
        self.assertEqual(decision.price, "18.90")

    def test_uses_inner_roll_count_for_nested_trash_bag_case(self) -> None:
        product = self.make_product(
            sku="48237D",
            item_name="Kehrichtsack, 110 L Quick-Bag, schwarz - 110 Liter",
            price="104.10",
            vessel_size="110",
            vessel_unit="l",
            raw_detail_price_unit_text="20 Rollen / Karton",
            raw_spec_piece_text="Rolle à 10 Säcke, 20 Rollen / Karton",
            raw_fill_text="110 Liter",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "preserve_inner_unit_divide_price")
        self.assertEqual(decision.vessel_size, "10")
        self.assertEqual(decision.vessel_unit, "quantity")
        self.assertEqual(decision.vessel_type, "roll")
        self.assertEqual(decision.bundle_size, "20")
        self.assertEqual(decision.bundle_type, "Karton")
        self.assertEqual(decision.price, "5.21")

    def test_treats_gn_container_capacity_as_descriptive(self) -> None:
        product = self.make_product(
            sku="GN11020",
            item_name="GN-Behälter, GN 1/1, 20 mm, 2.5 Liter, Edelstahl",
            price="29.20",
            vessel_size="2.5",
            vessel_unit="l",
            raw_detail_price_unit_text="1 Stück",
            raw_spec_piece_text="1 Stück",
            raw_fill_text="2.5 Liter",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "piece_unit_capacity_descriptive")
        self.assertEqual(decision.vessel_size, "1")
        self.assertEqual(decision.vessel_unit, "quantity")
        self.assertEqual(decision.vessel_type, "container")
        self.assertEqual(decision.bundle_size, "")

    def test_reads_count_only_volume_multipack_from_anzahl(self) -> None:
        product = self.make_product(
            sku="25143W",
            item_name='Cremeseife "Fleurelle", pH neutral, 8 x 1\'000 ml / Karton',
            price="26.40",
            vessel_size="1000",
            vessel_unit="ml",
            raw_spec_piece_text="8 x 1'000 ml / Karton",
            raw_fill_text="1'000 ml",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "preserve_inner_unit_divide_price")
        self.assertEqual(decision.vessel_size, "1000")
        self.assertEqual(decision.vessel_unit, "ml")
        self.assertEqual(decision.bundle_size, "8")
        self.assertEqual(decision.bundle_type, "Karton")
        self.assertEqual(decision.price, "3.30")

    def test_preserves_bag_in_box_single_unit_when_detail_overrides_carton_spec(self) -> None:
        product = self.make_product(
            sku="400090x",
            item_name="Seife PEARL 900 ml für Spender Bag-in-Box, kl.M.",
            price="8.45",
            vessel_size="1000",
            vessel_unit="ml",
            vessel_type="box",
            raw_detail_price_unit_text="1 Stück",
            raw_spec_piece_text="6 Stück / Karton",
            raw_fill_text="1000 ml",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "preserve_single_named_unit")
        self.assertEqual(decision.vessel_size, "1000")
        self.assertEqual(decision.vessel_unit, "ml")
        self.assertEqual(decision.vessel_type, "bag")
        self.assertEqual(decision.bundle_size, "")
        self.assertEqual(decision.price, "8.45")

    def test_divides_bag_in_box_carton_price(self) -> None:
        product = self.make_product(
            sku="400090",
            item_name="Seife PEARL 900 ml für Spender Bag-in-Box - Standardmenge",
            price="94.00",
            raw_detail_price_unit_text="12 Stück / Karton",
            raw_spec_piece_text="6 Stück / Karton",
            raw_fill_text="1000 ml",
        )
        decision = interpret_packaging(product)
        self.assertEqual(decision.mode, "preserve_inner_unit_divide_price")
        self.assertEqual(decision.vessel_size, "1000")
        self.assertEqual(decision.vessel_unit, "ml")
        self.assertEqual(decision.vessel_type, "bag")
        self.assertEqual(decision.bundle_size, "6")
        self.assertEqual(decision.bundle_type, "Karton")
        self.assertEqual(decision.price, "15.67")


if __name__ == "__main__":
    unittest.main()
