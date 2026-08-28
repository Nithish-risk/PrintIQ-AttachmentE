import unittest
from modules.matcher_guard import identity_evidence, validate_record

class Tests(unittest.TestCase):
    def test_rejects_subset_date_collision(self):
        r={'item':'DATE LAST MARRIAGE ENDED (or Date of Death)','section':'ELIGIBILITY','subsection':'PARTY B'}
        f={'di_key':'DATE OF MARRIAGE','di_section':'MARRIAGE','di_subsection':'OFFICIANT'}
        self.assertFalse(identity_evidence(r,f)['confident'])

    def test_accepts_exact(self):
        r={'item':'DATE OF BIRTH','section':'LICENSE - PARTY A','subsection':'GROOM/SPOUSE'}
        f={'di_key':'DATE OF BIRTH','di_section':'LICENSE - PARTY A','di_subsection':'GROOM/SPOUSE'}
        self.assertTrue(identity_evidence(r,f)['confident'])

    def test_score_capped(self):
        x=validate_record({'matched':True,'match_score':122,'rule_type':'FIELD_TEXT','item':'COUNTRY','di_key':'COUNTRY'})
        self.assertEqual(x['match_score'],100.0)

if __name__=='__main__': unittest.main()
