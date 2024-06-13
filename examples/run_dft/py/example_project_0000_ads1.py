from gpaw import GPAW, PW, Mixer, MixerDif, MixerSum, Davidson
from ase.io import read
from ase.db import connect
from ase.optimize import LBFGS
from cheatools.dftsampling import add_ads

atoms = read('../traj/example_project_0000_slab.traj',-1)

atoms = add_ads(atoms, 'fcc111', (3, 3, 5), 'fcc', 'O', 1.3, 1)
calc = GPAW(mode=PW(400), xc='RPBE', kpts=(4, 4, 1), eigensolver=Davidson(3), mixer=MixerDif(beta=0.05, nmaxold=5, weight=50.0), parallel={'augment_grids': True, 'sl_auto': True}, txt='../txt/example_project_0000_ads1.txt')
atoms.set_calculator(calc)
dyn = LBFGS(atoms, trajectory='../traj/example_project_0000_ads1.traj')
dyn.run(fmax = 0.1)
atoms.get_potential_energy()
connect('../example_project_fcc_O.db').write(atoms, slabId=0, adsId=1, arrayId=1)
