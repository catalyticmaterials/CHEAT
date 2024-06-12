import torch, ase.build
import numpy as np
from ase.neighborlist import build_neighbor_list, natural_cutoffs
from itertools import combinations
from utils.data import add_ads
from copy import deepcopy
from collections import Counter
from torch_geometric.data import Data
from ocpmodels.preprocessing import AtomsToGraphs

def ase2ocp_tags(atoms):
    """
    Converts ASE tag format to OCP tag format
    -------
    ASE format: 0 = Adsorbate, 1 = Surface, 2 = Subsurface, 3 = Third layer, ..
    OCP format: 2 = Adsorbate, 1 = Surface, 0 = Everything else, ...
    """
    atoms.set_tags([0 if t >= 2 else 2 if t == 0 else 1 for t in atoms.get_tags()])
    
    return atoms

def get_ensemble(atoms):
    """
    Get ensemble (surface elements bound to adsorbate) as well as site for fcc(111) surfaces.
    ---
    Only implemented for monodentate adsorbates on fcc(111) surfaces. 
    Will categorize sites in ontop, bridge_{0,1,2}, fcc, or hcp.  

    Returns
    -------
    dict
        Keys are elements found in the ensemble and values are the number of atoms found
    list
        IDs of ensemble atoms
    string
        Site category  
    """
    
    atoms = deepcopy(atoms)  # copy to not change tags on original atoms object 
    if np.any(np.isin([3,4,5], atoms.get_tags())):  # the following operations uses the ocp tag format
        atoms = ase2ocp_tags(atoms)
    
    # center adsorbate and wrap cell to handle adsorbates close to the periodic boundaries
    ads_ids = [a.index for a in atoms if a.tag == 2]
    cell_xy = 0.5*np.sum(atoms.get_cell()[:2],axis=0)
    ads_xy = atoms[ads_ids[0]].position
    adjust = -ads_xy+cell_xy
    atoms.translate([adjust[0],adjust[1],0])
    atoms.wrap() 
    
    # build neighborlist to assert bonds - uses 110% natural cutoffs to ensure connectivity in distorted structures
    nl = build_neighbor_list(atoms, cutoffs=natural_cutoffs(atoms, mult=1.1), self_interaction=False, bothways=True)
    ads_neighbors = np.array([i for i in nl.get_neighbors(ads_ids[0])[0] if i not in ads_ids])  # ids connected to the first adsorbate atom 
    if len(ads_neighbors) == 0:
        raise Exception("Adsorbate has no neighboring atoms.")  

    # we consider the three nearest atoms to be the potential ensemble  
    dist = atoms.get_distances(ads_ids[0],ads_neighbors)
    ens_ids = ads_neighbors[np.argsort(dist)][:3]
    
    # all possible ensembles given the three nearest atoms 
    ens = [[i] for i in ens_ids]+[[*i] for i in list(combinations(ens_ids, 2))]+[list(ens_ids)]
    
    # assert distances to all ensemble midpoint e.g. mean position of two atoms for bridge site etc. 
    pos = atoms.get_positions()
    dist = []
    for e in ens:        
        mean = np.mean(pos[e],axis=0)
        delta = pos[ads_ids[0]] - mean
        dist.append(np.sqrt(np.sum(delta**2)))  
    closest_ens = ens[np.argsort(dist)[0]]

    # categorize ensemble
    if len(closest_ens) == 1:
        site = 'ontop'
    
    # bridge sites are subcategorized based on direction
    elif len(closest_ens) == 2:
        # directions (both ways)
        directions = np.array([[1.0,0.0,0.0],[-1.0,0.0,0.0], # horizontal
                        [0.5,0.866,0.0],[-0.5,-0.866,0.0], # lower left to upper
                        [-0.5,0.866,0.0],[0.5,-0.866,0.0]]) # lower right to upper
        
        delta = pos[closest_ens[0]] - pos[closest_ens[1]]  # vector between ensemble atoms
        delta = delta/np.sqrt(np.sum(delta**2))  # normalize to unit vector
        direction_id = int(np.floor(np.argmin(np.sum(np.abs(delta - directions),axis=1))/2))  # closest direction is chosen
        site = f'bridge_{direction_id}'
      
    elif len(closest_ens) == 3:
        # get three closest subsurface neighbors of the ensemble atoms
        neighbor_arr = []
        subsurface = np.array([a.index for a in atoms if a.tag == 0])
        for i in closest_ens:
            close_subsurface = np.argsort(atoms.get_distances(i,subsurface,mic=True))[:3] # minimum image convention to avoid pbc problems
            neighbor_arr.append(subsurface[close_subsurface])
        neighbor_arr = np.array(neighbor_arr)
        # if ensemble atoms share a subsurface neighbor, the site is hcp 
        site = 'fcc'
        for i in np.unique(neighbor_arr):
            if np.all(np.any(neighbor_arr == i, axis=1)):
                site = 'hcp'
                break
    
    # get elements of ensemble
    ensemble = np.array(atoms.get_chemical_symbols())[closest_ens]     
    ensemble = dict(Counter(ensemble))
    
    return ensemble, closest_ens, site

def atoms2template(atoms, tag_style='ocp'):
    """
    Transforms atoms object to template (set structure for each adsorbate/site combination)
    ---
    Only implemented for 3x3x5 atom-sized fcc(111) objects. Incompatible with any other size/surface.  
    Templates are structured based on a 3x3x5 atom-sized fcc(111) surface with lattice parameter (a) 3.9 and 10Å vacuum added above and below.     
    Bonds lengths for sites are {ontop:2.0, bridge:1.8, fcc:1.3, hcp:1.5}

    Returns
    -------
    Atoms object
        Template object  
    """
    # get ensemble ids and site to assert rolls and rotations
    ens, ids, site = get_ensemble(atoms)
    
    # get adsorbate to add to template
    if np.any(np.isin([3,4,5], atoms.get_tags())):
        ads_ids = [a.index for a in atoms if a.tag == 0]
    else:
        ads_ids = [a.index for a in atoms if a.tag == 2]
    ads = ''.join(np.array(atoms.get_chemical_symbols())[ads_ids])
    
    # ontop roll/rotate scheme
    if site == 'ontop':
        roll_1 = 1 if ids[0] < 39 else -1 if ids[0] > 41 else 0
        roll_2 = 1 if ids[0] in [36,39,42] else -1 if ids[0] in [38,41,44] else 0
        rotate_scheme = 0
    
    # bridge roll/rotate schemes (depending on direction)
    elif site[:-2] == 'bridge':
        rotate_scheme = int(site[-1])
        if np.all(np.isin([42,38],ids)): # upper corners
            ll = 42
            
        elif np.any(np.isin([36,39,42],ids)) and np.any(np.isin([38,41,44],ids)): # right side
            if rotate_scheme == 2:
                ll = np.min([id for id in ids if id in [36,39,42]])
            else: 
                ll = np.min([id for id in ids if id in [38,41,44]])
            
        elif np.any(np.isin([42,43,44],ids)) and np.any(np.isin([36,37,38],ids)): # upper side
            if rotate_scheme == 1:
                ll = np.min([id for id in ids if id in [42,43,44]])
            else:
                ll = np.min([id for id in ids if id in [36,37,38]])

        else:
            ll = np.min(ids)
        roll_1 = 1 if ll < 39 else -1 if ll > 41 else 0
        roll_2 = 1 if ll in [36,39,42] else -1 if ll in [38,41,44] else 0
    
    # fcc roll/rotate scheme
    elif site == 'fcc': 
        if np.all(np.isin([44,42,38],ids)):
            ll = 44
        elif np.any(np.isin([36,39,42],ids)) and np.any(np.isin([38,41,44],ids)): 
            ll = np.min([id for id in ids if id in [38,41,44]])
        elif np.any(np.isin([42,43,44],ids)) and np.any(np.isin([36,37,38],ids)):
            ll = np.min([id for id in ids if id in [42,43,44]])
        else:
            ll = np.min(ids)

        roll_1 = 1 if ll < 39 else -1 if ll > 41 else 0
        roll_2 = 1 if ll in [36,39,42] else -1 if ll in [38,41,44] else 0
        rotate_scheme = 0
    
    # hcp roll/rotate scheme
    elif site == 'hcp': 
        if np.all(np.isin([44,42,38],ids)):
            ll = 44
        elif np.any(np.isin([36,39,42],ids)) and np.any(np.isin([38,41,44],ids)): 
            ll = np.min([id for id in ids if id in [36,39,42]])
        elif np.any(np.isin([42,43,44],ids)) and np.any(np.isin([36,37,38],ids)):
            ll = np.min([id for id in ids if id in [42,43,44]])
        else:
            ll = np.min(ids)

        roll_1 = 1 if ll < 39 else -1 if ll > 41 else 0
        roll_2 = 1 if ll in [36,39,42] else -1 if ll in [38,41,44] else 0
        rotate_scheme = 0
    
    # roll template ids
    tpl = np.arange(45).reshape((5,3,3))    
    tpl = np.roll(tpl,roll_1,1)
    tpl = np.roll(tpl,roll_2,2)

    # if necessary rotate and roll template ids
    if rotate_scheme == 1:
        tpl = np.rot90(tpl, k=-1, axes=(1, 2))
        tpl[:,1,:] = np.roll(tpl[:,1,:],1,1)
        tpl[:,0,:] = np.roll(tpl[:,0,:],2,1)
        tpl[-2,:,:] = np.roll(tpl[-2,:,:],-1,0)
        tpl[-3,:,:] = np.roll(tpl[-3,:,:],-1,1)
        tpl[-5,:,:] = np.roll(tpl[-5,:,:],-1,0)

    elif rotate_scheme == 2:
        tpl = np.rot90(tpl, k=1, axes=(1, 2))
        tpl[:,:,0] = np.roll(tpl[:,:,0],1,1)
        tpl[:,:,2] = np.roll(tpl[:,:,2],2,1)
        tpl[-2,:,:] = np.roll(tpl[-2,:,:],-1,0)
        tpl[-2,:,:] = np.roll(tpl[-2,:,:],1,1)
        tpl[-3,:,:] = np.roll(tpl[-3,:,:],-1,0)
        tpl[-5,:,:] = np.roll(tpl[-5,:,:],-1,0)
        tpl[-5,:,:] = np.roll(tpl[-5,:,:],1,1)
    
    # make template atoms object and assign symbols from rolled ids 
    template = ase.build.fcc111('Au', size=(3,3,5), vacuum=10, a=3.9)
    template.set_chemical_symbols(np.array(atoms.get_chemical_symbols())[tpl.ravel()])
    
    # add adsorbate based on site details
    site = 'bridge' if site[:-2] == 'bridge' else site
    ads_id = 3 if site == 'hcp' else 4
    height = {'ontop':2.0,'bridge':1.8,'fcc':1.3,'hcp':1.5}
    new_atoms = add_ads(template, 'fcc111', (3,3,5), site, ads, height[site], ads_id)
    
    # adjust tag style
    if tag_style == 'ase':
        atoms.set_tags([i+1 for i in range(5)[::-1] for j in range(9)] + [0] * len(ads))
    elif tag_style == 'ocp':
        template = ase2ocp_tags(template)

    return template

def atoms2graph(atoms, onehot_labels):
    ens, _, site = get_ensemble(atoms)
    atoms = atoms2template(atoms, tag_style='ase')
    del atoms[[a.index for a in atoms if a.tag > 3]]
    
    nl = build_neighbor_list(atoms, cutoffs=natural_cutoffs(atoms, mult=1.1), self_interaction=False, bothways=True)
    all_edges = np.array([[a.index,i] for a in atoms for i in nl.get_neighbors(a.index)[0]])
    
    if site == 'ontop':
        aoi = [0,2,6]
    elif site == 'fcc':
        aoi = [0]
    elif site == 'hcp':
        aoi = [0,1,3]
    elif 'bridge' in site:
        aoi = [0,6,7]

    # onehot encoding of the node list
    node_onehot = np.zeros((len(atoms), len(onehot_labels) + 2))
    for a in atoms:
        node_onehot[a.index, onehot_labels.index(a.symbol)] = 1
        node_onehot[a.index, -2] = a.tag
        if a.index in aoi:
            node_onehot[a.index, -1] = 1
    
    # find adsorbate 
    ads = ''.join(np.array(atoms.get_chemical_symbols())[[a.index for a in atoms if a.tag == 0]])
    
    # make torch data object
    torch_edges = torch.tensor(np.transpose(all_edges), dtype=torch.long)
    torch_nodes = torch.tensor(node_onehot, dtype=torch.float)

    graph = Data(x=torch_nodes, edge_index=torch_edges, onehot_labels=onehot_labels, ads=ads)
    return graph

class templater():
    def __init__(self,template,facet,adsorbates,sites,onehot_labels=None):
        self.template, self.template_dict = template, {}
        height = {'ontop':2.0,'bridge':1.8,'fcc':1.3,'hcp':1.5}
        atoms = ase.build.fcc111(onehot_labels[0] if template == 'lgnn' else 'Au', size=(3,3,5), vacuum=10, a=3.9)
        a2g = AtomsToGraphs()
        for ads, site in zip(adsorbates,sites):
            ads_id = 3 if site == 'hcp' else 4
            temp_atoms = add_ads(deepcopy(atoms), 'fcc111', (3,3,5), site, ads, height[site], ads_id)
            
            if 'ocp' in template:
                if template == 'shallow_ocp':
                    del temp_atoms[[atom.index for atom in temp_atoms if atom.tag in [4,5]]]
                temp_atoms = ase2ocp_tags(temp_atoms)
                data_object = a2g.convert_all([temp_atoms], disable_tqdm=True)[0]
            
            if template == 'lgnn':
                data_object = atoms2graph(temp_atoms, onehot_labels)
                data_object.x[:,0] = 0

            self.template_dict[(ads,site)] = data_object   
            
    def fill_template(self,symbols,adsorbate,site):
        cell = deepcopy(self.template_dict[(adsorbate,site)])
        if 'ocp' in self.template:
            cell.atomic_numbers[:len(symbols)] = torch.tensor([ase.data.atomic_numbers[s] for s in symbols])
            #cell.sid = 0 # UNCOMMENT THIS If TROUBLE
        elif self.template == 'lgnn':
            for i, s in enumerate(symbols):
                cell.x[i, cell.onehot_labels.index(s)] = 1
        return cell
"""
def get_lgnn_templates(facet, adsorbates, sites, onehot_labels):
    templates = {}
    height = {'ontop':2.0,'bridge':1.8,'fcc':1.3,'hcp':1.5}
    atoms = ase.build.fcc111(onehot_labels[0], size=(3,3,5), vacuum=10, a=3.9)
    for ads, site in zip(adsorbates,sites):
        ads_id = 3 if site == 'hcp' else 4
        temp_atoms = add_ads(deepcopy(atoms), 'fcc111', (3,3,5), site, ads, height[site], ads_id)
        data_object = atoms2graph(temp_atoms, onehot_labels)
        data_object.x[:,0] = 0
        templates[(ads,site)] = data_object
    return templates

def get_ocp_templates(facet,adsorbates,sites):
    templates = {}
    height = {'ontop':2.0,'bridge':1.8,'fcc':1.3,'hcp':1.5}
    atoms = ase.build.fcc111('Au', size=(3,3,5), vacuum=10, a=3.9)
    a2g = AtomsToGraphs()
    for ads, site in zip(adsorbates,sites):
        ads_id = 3 if site == 'hcp' else 4
        temp_atoms = add_ads(deepcopy(atoms), 'fcc111', (3,3,5), site, ads, height[site], ads_id)
        temp_atoms = ase2ocp_tags(temp_atoms)
        data_object = a2g.convert_all([temp_atoms], disable_tqdm=True)[0]
        templates[(ads,site)] = data_object
    return templates

def get_shallow_ocp_templates(facet,adsorbates,sites):
    templates = {}
    height = {'ontop':2.0,'bridge':1.8,'fcc':1.3,'hcp':1.5}
    atoms = ase.build.fcc111('Au', size=(3,3,5), vacuum=10, a=3.9)
    a2g = AtomsToGraphs()
    for ads, site in zip(adsorbates,sites):
        ads_id = 3 if site == 'hcp' else 4
        tpl = add_ads(deepcopy(atoms), 'fcc111', (3,3,5), site, ads, height[site], ads_id)
        del temp_atoms[[atom.index for atom in temp_atoms if atom.tag in [4,5]]]
        temp_atoms = ase2ocp_tags(temp_atoms)
        data_object = a2g.convert_all([temp_atoms], disable_tqdm=True)[0]
        templates[(ads,site)] = data_object
    return templates
"""