from modules.coordinate_utils import center_inside, overlap_ratio

def test_center_inside():
    assert center_inside([2,2,4,4], [0,0,10,10])
    assert overlap_ratio([0,0,5,5], [0,0,5,5]) == 1
