"""Intra- and inter-subdomain geometry processing."""

__author__ = 'Michal Januszewski'
__email__ = 'sailfish-cfd@googlegroups.com'
__license__ = 'LGPL3'

from collections import defaultdict, namedtuple
import inspect
import operator
import numpy as np
from scipy.ndimage import filters

from sailfish import util
from sailfish import sym
import sailfish.node_type as nt
from sailfish.subdomain_connection import LBConnection

ConnectionPair = namedtuple('ConnectionPair', 'src dst')

# Used for creating connections between subdomains.  Without PBCs,
# virtual == real.  With PBC, the real subdomain is the actual subdomain
# as defined by the simulation geometry, and the virtual subdomain is a
# copy created due to PBC.
SubdomainPair = namedtuple('SubdomainPair', 'real virtual')


class SubdomainSpec(object):
    """A lightweight class describing the location of a subdomain and its
    connections to other subdomains in the simulation.

    This class does not contain any references to the actual GPU or host data
    structures necessary to run the simulation for this subdomain.
    """

    dim = None

    # Face IDs.
    X_LOW = 0
    X_HIGH = 1
    Y_LOW = 2
    Y_HIGH = 3
    Z_LOW = 4
    Z_HIGH = 5

    def __init__(self, location, size, envelope_size=None, id_=None, *args, **kwargs):
        self.location = location
        self.size = size

        if envelope_size is not None:
            self.set_actual_size(envelope_size)
        else:
            # Actual size of the simulation domain, including the envelope (ghost
            # nodes).  This is set later when the envelope size is known.
            self.actual_size = None
            self.envelope_size = None
        self._runner = None
        self._id = id_
        self._clear_connections()
        self._clear_connectors()

        self.vis_buffer = None
        self.vis_geo_buffer = None
        self._periodicity = [False] * self.dim

    def __repr__(self):
        return '{0}({1}, {2}, id_={3})'.format(self.__class__.__name__,
                self.location, self.size, self._id)

    @property
    def runner(self):
        return self._runner

    @runner.setter
    def runner(self, x):
        self._runner = x

    @property
    def id(self):
        return self._id

    @id.setter
    def id(self, x):
        self._id = x

    @property
    def num_nodes(self):
        return reduce(operator.mul, self.size)

    @property
    def periodic_x(self):
        """X-axis periodicity within this subdomain."""
        return self._periodicity[0]

    @property
    def periodic_y(self):
        """Y-axis periodicity within this subdomain."""
        return self._periodicity[1]

    def update_context(self, ctx):
        ctx['dim'] = self.dim
        # The flux tensor is a symmetric matrix.
        ctx['flux_components'] = self.dim * (self.dim + 1) / 2
        ctx['envelope_size'] = self.envelope_size
        # TODO(michalj): Fix this.
        # This requires support for ghost nodes in the periodicity code
        # on the GPU.
        # ctx['periodicity'] = self._periodicity
        ctx['periodicity'] = [False, False, False]
        ctx['periodic_x'] = 0 #int(self._block.periodic_x)
        ctx['periodic_y'] = 0 #int(self._block.periodic_y)
        ctx['periodic_z'] = 0 #periodic_z

    def enable_local_periodicity(self, axis):
        """Makes the subdomain locally periodic along a given axis."""
        assert axis <= self.dim-1
        self._periodicity[axis] = True
        # TODO: As an optimization, we could drop the ghost node layer in this
        # case.

    def _add_connection(self, face, cpair):
        if cpair in self._connections[face]:
            return
        self._connections[face].append(cpair)

    def _clear_connections(self):
        self._connections = defaultdict(list)

    def _clear_connectors(self):
        self._connectors = {}

    def add_connector(self, subdomain_id, connector):
        assert subdomain_id not in self._connectors
        self._connectors[subdomain_id] = connector

    def get_connection(self, face, subdomain_id):
        """Returns a LBConnection object describing the connection to 'subdomain_id'
        via 'face'."""
        try:
            for pair in self._connections[face]:
                if pair.dst.block_id == subdomain_id:
                    return pair
        except KeyError:
            pass

    def get_connections(self, face, subdomain_id):
        ret = []
        for pair in self._connections[face]:
            if pair.dst.block_id == subdomain_id:
                ret.append(pair)
        return ret

    def connecting_subdomains(self):
        """Returns a list of pairs: (face, subdomain ID) representing connections
        to different subdomains."""
        ids = set([])
        for face, v in self._connections.iteritems():
            for pair in v:
                ids.add((face, pair.dst.block_id))
        return list(ids)

    def has_face_conn(self, face):
        return face in self._connections.keys()

    def set_actual_size(self, envelope_size):
        # TODO: It might be possible to optimize this a little by avoiding
        # having buffers on the sides which are not connected to other subdomains.
        self.actual_size = [x + 2 * envelope_size for x in self.size]
        self.envelope_size = envelope_size

    def set_vis_buffers(self, vis_buffer, vis_geo_buffer):
        self.vis_buffer = vis_buffer
        self.vis_geo_buffer = vis_geo_buffer

    @classmethod
    def face_to_dir(cls, face):
        if face in (cls.X_LOW, cls.Y_LOW, cls.Z_LOW):
            return -1
        else:
            return 1

    @classmethod
    def face_to_axis(cls, face):
        """Returns the axis number corresponding to a face constant."""
        if face == cls.X_HIGH or face == cls.X_LOW:
            return 0
        elif face == cls.Y_HIGH or face == cls.Y_LOW:
            return 1
        elif face == cls.Z_HIGH or face == cls.Z_LOW:
            return 2

    def face_to_normal(self, face):
        """Returns the normal vector for a face."""
        comp = self.face_to_dir(face)
        pos  = self.face_to_axis(face)
        direction = [0] * self.dim
        direction[pos] = comp
        return direction

    def opposite_face(self, face):
        opp_map = {
            self.X_HIGH: self.X_LOW,
            self.Y_HIGH: self.Y_LOW,
            self.Z_HIGH: self.Z_LOW
        }
        opp_map.update(dict((v, k) for k, v in opp_map.iteritems()))
        return opp_map[face]

    @classmethod
    def axis_dir_to_face(cls, axis, dir_):
        if axis == 0:
            if dir_ == -1:
                return cls.X_LOW
            elif dir_ == 1:
                return cls.X_HIGH
        elif axis == 1:
            if dir_ == -1:
                return cls.Y_LOW
            elif dir_ == 1:
                return cls.Y_HIGH
        elif axis == 2:
            if dir_ == -1:
                return cls.Z_LOW
            elif dir_ == -1:
                return cls.Z_HIGH

    def connect(self, pair, grid=None):
        """Creates a connection between this subdomain and another subdomain.

        A connection can only be created when the subdomains are next to each
        other.

        :returns: True if the connection was successful
        :rtype: bool
        """
        # Convenience helper for tests.
        if type(pair) is not SubdomainPair:
            pair = SubdomainPair(pair, pair)

        assert pair.real.id != self.id

        def connect_x(r1, r2, v1, v2):
            c1 = LBConnection.make(v1, v2, self.X_HIGH, grid)
            c2 = LBConnection.make(v2, v1, self.X_LOW, grid)

            if c1 is None:
                return False

            r1._add_connection(self.X_HIGH, ConnectionPair(c1, c2))
            r2._add_connection(self.X_LOW, ConnectionPair(c2, c1))
            return True

        def connect_y(r1, r2, v1, v2):
            c1 = LBConnection.make(v1, v2, self.Y_HIGH, grid)
            c2 = LBConnection.make(v2, v1, self.Y_LOW, grid)

            if c1 is None:
                return False

            r1._add_connection(self.Y_HIGH, ConnectionPair(c1, c2))
            r2._add_connection(self.Y_LOW, ConnectionPair(c2, c1))
            return True

        def connect_z(r1, r2, v1, v2):
            c1 = LBConnection.make(v1, v2, self.Z_HIGH, grid)
            c2 = LBConnection.make(v2, v1, self.Z_LOW, grid)

            if c1 is None:
                return False

            r1._add_connection(self.Z_HIGH, ConnectionPair(c1, c2))
            r2._add_connection(self.Z_LOW, ConnectionPair(c2, c1))
            return True

        if self.ex == pair.virtual.ox:
            return connect_x(self, pair.real, self, pair.virtual)
        elif pair.virtual.ex == self.ox:
            return connect_x(pair.real, self, pair.virtual, self)
        elif self.ey == pair.virtual.oy:
            return connect_y(self, pair.real, self, pair.virtual)
        elif pair.virtual.ey == self.oy:
            return connect_y(pair.real, self, pair.virtual, self)
        elif self.dim == 3:
            if self.ez == pair.virtual.oz:
                return connect_z(self, pair.real, self, pair.virtual)
            elif pair.virtual.ez == self.oz:
                return connect_z(pair.real, self, pair.virtual, self)

        return False

class SubdomainSpec2D(SubdomainSpec):
    dim = 2

    def __init__(self, location, size, envelope_size=None, *args, **kwargs):
        self.ox, self.oy = location
        self.nx, self.ny = size
        self.ex = self.ox + self.nx
        self.ey = self.oy + self.ny
        self.end_location = [self.ex, self.ey]  # first node outside the subdomain
        SubdomainSpec.__init__(self, location, size, envelope_size, *args, **kwargs)

    @property
    def _nonghost_slice(self):
        """Returns a 2-tuple of slice objects that selects all non-ghost nodes."""

        es = self.envelope_size
        return (slice(es, es + self.ny), slice(es, es + self.nx))


class SubdomainSpec3D(SubdomainSpec):
    dim = 3

    def __init__(self, location, size, envelope_size=None, *args, **kwargs):
        self.ox, self.oy, self.oz = location
        self.nx, self.ny, self.nz = size
        self.ex = self.ox + self.nx
        self.ey = self.oy + self.ny
        self.ez = self.oz + self.nz
        self.end_location = [self.ex, self.ey, self.ez]  # first node outside the subdomain
        self._periodicity = [False, False, False]
        SubdomainSpec.__init__(self, location, size, envelope_size, *args, **kwargs)

    @property
    def _nonghost_slice(self):
        """Returns a 3-tuple of slice objects that selects all non-ghost nodes."""
        es = self.envelope_size
        return (slice(es, es + self.nz), slice(es, es + self.ny), slice(es, es + self.nx))

    @property
    def periodic_z(self):
        """Z-axis periodicity within this subdomain."""
        return self._periodicity[2]


class Subdomain(object):
    """Holds all field and geometry information specific to the subdomain
    described by the corresponding SubdomainSpec."""

    NODE_MISC_MASK = 0
    NODE_MISC_SHIFT = 1
    NODE_TYPE_MASK = 2

    @classmethod
    def add_options(cls, group):
        pass

    def __init__(self, grid_shape, spec, grid, *args, **kwargs):
        """
        :param grid_shape: size of the lattice for the subdomain, including
                ghost nodes; X dimension is the last element in the tuple
        :param spec: SubdomainSpec for this subdomain
        :param grid: grid object specifying the connectivity of the lattice
        """
        self.spec = spec
        self.grid_shape = grid_shape
        self.grid = grid
        # The type map allocated by the subdomain runner already includes
        # ghost nodes, and is formatted in a way that makes it suitable
        # for copying to the compute device. The entries in this array are
        # node type IDs.
        self._type_map = spec.runner.make_scalar_field(np.uint32, register=False)
        self._type_vis_map = np.zeros(list(reversed(spec.size)),
                dtype=np.uint8)
        self._type_map_encoded = False
        self._param_map = spec.runner.make_scalar_field(dtype=np.int_,
                register=False)
        self._params = {}
        self._encoder = None
        self._seen_types = set([0])
        self._needs_orientation = False
        self._orientation = spec.runner.make_scalar_field(np.uint32,
                register=False)

    @property
    def config(self):
        return self.spec.runner.config

    def boundary_conditions(self, *args):
        raise NotImplementedError('boundary_conditions() not defined in a child'
                ' class.')

    def initial_conditions(self, sim, *args):
        raise NotImplementedError('initial_conditions() not defined in a child '
                'class')

    def _verify_params(self, where, node_type):
        """Verifies that the node parameters are set correctly."""

        for name, param in node_type.params.iteritems():
            # Single number.
            if util.is_number(param):
                continue
            # Single vector.
            elif type(param) is tuple:
                for el in param:
                    if not util.is_number(el):
                        raise ValueError("Tuple elements have to be numbers.")
            # Field.  If more than a single number is needed per node, this
            # needs to be a numpy record array.  Use node_util.multifield()
            # to create this array easily.
            elif isinstance(param, np.ndarray):
                assert param.size == np.sum(where), ("Your array needs to "
                        "have exactly as many nodes as there are True values "
                        "in the 'where' array.  Use node_util.multifield() to "
                        "generate the array in an easy way.")
            elif isinstance(param, nt.DynamicValue):
                if param.has_symbols(sym.S.time):
                    self.config.time_dependence = True
                if param.has_symbols(sym.S.gx, sym.S.gy, sym.S.gz):
                    self.config.space_dependence = True
                continue
            else:
                raise ValueError("Unrecognized node param: {0} (type {1})".
                        format(name, type(param)))

    def set_node(self, where, node_type):
        """Set a boundary condition at selected node(s).

        :param where: index expression selecting nodes to set
        :param node_type: LBNodeType subclass or instance
        """
        assert not self._type_map_encoded
        if inspect.isclass(node_type):
            assert issubclass(node_type, nt.LBNodeType)
            node_type = node_type()
        else:
            assert isinstance(node_type, nt.LBNodeType)

        self._verify_params(where, node_type)
        self._type_map[where] = node_type.id
        key = hash((node_type.id, frozenset(node_type.params.items())))
        assert np.all(self._param_map[where] == 0),\
                "Overriding previously set nodes is not allowed."
        self._param_map[where] = key
        self._params[key] = node_type
        self._seen_types.add(node_type.id)

        if hasattr(node_type, 'orientation') and node_type.orientation is not None:
            self._orientation[where] = node_type.orientation
        elif node_type.needs_orientation:
            self._needs_orientation = True

    def update_node(self, where, node_type):
        """Updates a boundary condition at selected node(s).

        Use this method only to update nodes in a _running_ simulation.
        See set_node for a description of params.
        """
        if inspect.isclass(node_type):
            assert issubclass(node_type, nt.LBNodeType)
            node_type = node_type()
        else:
            assert isinstance(node_type, nt.LBNodeType)

        if not self._type_map_encoded:
            raise ValueError('Simulation not started. Use set_node instead.')

        key = hash((node_type.id, frozenset(node_type.params.items())))
        if key not in self._params:
            raise ValueError('Setting nodes with new parameters is not '
                             'supported.')

        if node_type.needs_orientation and (not hasattr(node_type, 'orientation')
                                            or node_type.orientation is None):
            raise ValueError('Node orientation not specified.')

        self._type_map[where] = self._encoder._subdomain_encode_node(
            getattr(node_type, 'orientation', 0),
            node_type.id, key)

    def reset(self):
        self.config.logger.debug('Setting subdomain geometry...')
        self._type_map_encoded = False
        mgrid = self._get_mgrid()
        self.boundary_conditions(*mgrid)
        self.config.logger.debug('... boundary conditions done.')

        self._postprocess_nodes()
        self.config.logger.debug('... postprocessing done.')
        self._define_ghosts()
        self.config.logger.debug('... ghosts done.')

        # Cache the unencoded type map for visualization.
        self._type_vis_map[:] = self._type_map[:]

        # TODO: At this point, we should decide which GeoEncoder class to use.
        from sailfish import geo_encoder
        self._encoder = geo_encoder.GeoEncoderConst(self)
        self._encoder.prepare_encode(self._type_map.base, self._param_map.base,
                                     self._params)

        self.config.logger.debug('... encoder done.')

    @property
    def scratch_space_size(self):
        """Node scratch space size expressed in number of floating point values."""
        return self._encoder.scratch_space_size if self._encoder is not None else 0

    def init_fields(self, sim):
        mgrid = self._get_mgrid()
        self.initial_conditions(sim, *mgrid)

    def update_context(self, ctx):
        assert self._encoder is not None
        self._encoder.update_context(ctx)
        ctx['x_local_device_to_global_offset'] = self.spec.ox - self.spec.envelope_size
        ctx['y_local_device_to_global_offset'] = self.spec.oy - self.spec.envelope_size
        if self.dim == 3:
            ctx['z_local_device_to_global_offset'] = self.spec.oz - self.spec.envelope_size

    def encoded_map(self):
        if not self._type_map_encoded:
            self._encoder.encode(self._orientation.base, self._needs_orientation)
            self._type_map_encoded = True

        return self._type_map.base

    def visualization_map(self):
        """Returns an unencoded type map for visualization/
        postprocessing purposes."""
        return self._type_vis_map

    def fluid_map(self):
        fm = self.visualization_map()
        uniq_types = set(np.unique(fm))
        wet_types = list(set(nt.get_wet_node_type_ids()) & uniq_types)
        wet_types = self._type_map.dtype.type(wet_types)
        return util.in_anyd_fast(fm, wet_types)

class Subdomain2D(Subdomain):
    dim = 2

    def __init__(self, grid_shape, spec, *args, **kwargs):
        self.gy, self.gx = grid_shape
        Subdomain.__init__(self, grid_shape, spec, *args, **kwargs)

    def _get_mgrid(self):
        return reversed(np.mgrid[self.spec.oy:self.spec.oy + self.spec.ny,
                                 self.spec.ox:self.spec.ox + self.spec.nx])

    def _define_ghosts(self):
        assert not self._type_map_encoded
        es = self.spec.envelope_size
        if not es:
            return
        self._type_map.base[0:es, :] = nt._NTGhost.id
        self._type_map.base[:, 0:es] = nt._NTGhost.id
        self._type_map.base[es + self.spec.ny:, :] = nt._NTGhost.id
        self._type_map.base[:, es + self.spec.nx:] = nt._NTGhost.id

    def _postprocess_nodes(self):
        uniq_types = set(np.unique(self._type_map.base))
        dry_types = list(set(nt.get_dry_node_type_ids()) & uniq_types)
        dry_types = self._type_map.dtype.type(dry_types)

        # Find nodes which are walls themselves and are completely surrounded by
        # walls.  These nodes are marked as unused, as they do not contribute to
        # the dynamics of the fluid in any way.
        cnt = np.zeros_like(self._type_map.base).astype(np.uint32)
        for i, vec in enumerate(self.grid.basis):
            a = np.roll(self._type_map.base, int(-vec[0]), axis=1)
            a = np.roll(a, int(-vec[1]), axis=0)
            cnt[util.in_anyd_fast(a, dry_types)] += 1

        self._type_map.base[(cnt == self.grid.Q)] = nt._NTUnused.id

class Subdomain3D(Subdomain):
    dim = 3

    def __init__(self, grid_shape, spec, *args, **kwargs):
        self.gz, self.gy, self.gx = grid_shape
        Subdomain.__init__(self, grid_shape, spec, *args, **kwargs)

    def _get_mgrid(self):
        return reversed(np.mgrid[self.spec.oz:self.spec.oz + self.spec.nz,
                                 self.spec.oy:self.spec.oy + self.spec.ny,
                                 self.spec.ox:self.spec.ox + self.spec.nx])

    def _define_ghosts(self):
        assert not self._type_map_encoded
        es = self.spec.envelope_size
        if not es:
            return
        self._type_map.base[0:es, :, :] = nt._NTGhost.id
        self._type_map.base[:, 0:es, :] = nt._NTGhost.id
        self._type_map.base[:, :, 0:es] = nt._NTGhost.id
        self._type_map.base[es + self.spec.nz:, :, :] = nt._NTGhost.id
        self._type_map.base[:, es + self.spec.ny:, :] = nt._NTGhost.id
        self._type_map.base[:, :, es + self.spec.nx:] = nt._NTGhost.id

    def _postprocess_nodes(self):
        uniq_types = set(np.unique(self._type_map.base))
        dry_types = list(set(nt.get_dry_node_type_ids()) & uniq_types)
        dry_types = self._type_map.dtype.type(dry_types)

        # Find nodes which are walls themselves and are completely surrounded by
        # walls. These nodes are marked as unused, as they do not contribute to
        # the dynamics of the fluid in any way.
        dry_map = util.in_anyd_fast(self._type_map.base,
                                    dry_types).astype(np.uint8)
        neighbors = np.zeros((3, 3, 3), dtype=np.uint8)
        neighbors[1,1,1] = 1
        for ei in self.grid.basis:
            neighbors[1 + ei[2], 1 + ei[1], 1 + ei[0]] = 1

        where = (filters.convolve(dry_map, neighbors, mode='wrap') == self.grid.Q)
        self._type_map.base[where] = nt._NTUnused.id
